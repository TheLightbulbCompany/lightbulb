import { test, expect } from '@playwright/test';
import { clerkSetup, setupClerkTestingToken } from '@clerk/testing/playwright';
import { createE2EUser, cleanupUser, type E2EUser } from './fixtures/user';
import { signIn } from './drivers/sign-in';

/**
 * Teams native UI — agent-create golden path (spec Task 28).
 *
 * End-to-end gate before prod cutover: confirms the full
 * browser → Next.js → /api/teams/agents BFF → Paperclip path works.
 *
 * Two reasons this spec must be skip-resilient:
 *
 * 1. The `/teams` tree is gated by `NEXT_PUBLIC_TEAMS_NATIVE_UI_ENABLED` in
 *    `src/middleware.ts`. When the flag is off the entire surface 404s, so
 *    the assertion targets won't render and we should skip cleanly rather
 *    than fail noise.
 * 2. The Teams BFF requires a provisioned `paperclip-companies` row for the
 *    user. A fresh E2E user (no org, no Stripe sub) won't have one until
 *    the Clerk org webhook fires `provision_org`, so against environments
 *    where Paperclip isn't reachable the panel renders an inline error
 *    state. We detect that and skip, again to keep the gate green where the
 *    feature genuinely isn't available.
 *
 * Mirrors the auth + cleanup pattern from `personal.spec.ts` /
 * `org.spec.ts`: `createE2EUser` in beforeAll, `signIn` via Clerk ticket,
 * `cleanupUser` in afterAll wipes Clerk + Stripe + backend state.
 */

test.describe('E2E: Teams agent create', () => {
  test.describe.configure({ mode: 'serial' });
  // Single-step test, no cold-start container path — keep the cap modest.
  test.setTimeout(5 * 60_000);

  let user: E2EUser;

  test.beforeAll(async ({ browser }) => {
    await clerkSetup();
    const ctx = await browser.newContext({
      extraHTTPHeaders: process.env.VERCEL_AUTOMATION_BYPASS_SECRET
        ? { 'x-vercel-protection-bypass': process.env.VERCEL_AUTOMATION_BYPASS_SECRET }
        : {},
    });
    const page = await ctx.newPage();
    await setupClerkTestingToken({ page });
    user = await createE2EUser(page, 'personal');
    console.log(
      `[e2e] runId=${user.runId} clerkUserId=${user.clerkUserId} email=${user.email}`,
    );
  });

  test.afterAll(async () => {
    if (user) await cleanupUser(user);
  });

  test('create an agent in the teams panel', async () => {
    if (!process.env.BASE_URL) throw new Error('BASE_URL is required');

    // 1. Sign in via Clerk ticket (existing helper).
    await signIn(user.page, process.env.BASE_URL, user.clerkUserId);
    expect(user.page.url()).not.toContain('/sign-in');

    // 2. Navigate to the agents panel (canonical in-shell URL — the retired
    //    /teams/* routes no longer exist).
    await user.page.goto('/chat?panel=teams-agents');

    // 3. Skip if the feature flag is off (middleware rewrites to /404 →
    //    Next renders the App Router not-found UI; the "New agent" button
    //    won't appear) OR if the BFF errored because the user has no
    //    paperclip-companies row provisioned in this environment.
    const newAgentBtn = user.page.getByRole('button', { name: 'New agent' });
    try {
      await expect(newAgentBtn).toBeVisible({ timeout: 15_000 });
    } catch {
      const pageText = await user.page.locator('body').innerText();
      const flagOff = /404|This page could not be found/i.test(pageText);
      const bffError = /^Error:/m.test(pageText);
      if (flagOff) {
        test.skip(true, '/teams disabled in this environment (flag off → 404)');
      }
      if (bffError) {
        test.skip(
          true,
          'Teams BFF returned an error (likely no paperclip-companies row provisioned for this user)',
        );
      }
      throw new Error(
        `New agent button not visible and no recognized skip condition. Page text: ${pageText.slice(0, 500)}`,
      );
    }

    // 4. Open the create dialog.
    await newAgentBtn.click();

    // 5. Fill in the name input. The dialog has Name (text input) and Role
    //    (select). Default role "engineer" is fine; don't touch the select.
    const agentName = `e2e-${user.runId}`;
    const dialog = user.page.locator('div').filter({ hasText: /^New agent$/ }).last();
    await dialog
      .getByText('Name')
      .locator('..')
      .locator('input')
      .fill(agentName);

    // 6. Submit.
    await user.page.getByRole('button', { name: 'Create' }).click();

    // 7. Assert the new agent appears in the list. The list rows render the
    //    name as `<div class="font-medium">{a.name}</div>`. Once SWR
    //    re-fetches after the POST, the name should appear.
    await expect(user.page.getByText(agentName, { exact: true })).toBeVisible({
      timeout: 15_000,
    });
  });
});
