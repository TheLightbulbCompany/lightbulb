import { expect, test } from '@playwright/test';
import { createSignInToken } from './fixtures/clerk-admin';

/**
 * Landing Funnel journey: the deferred-auth path shipped in the landing
 * rebuild. A visitor types their idea into the hero pill, is routed to
 * /sign-up with the brief stashed in sessionStorage, and after signup the
 * replay hook auto-submits the brief — the in-chat funnel must never appear,
 * and Friday's kickoff eventually lands the plan.
 *
 * Signup itself is programmatic (ticket strategy), matching the house rule in
 * drivers/sign-in.ts: Clerk's own form is Clerk's problem. What matters here
 * is that the SAME TAB carries the stash across the auth boundary.
 */

const BASE = process.env.BASE_URL ?? 'https://dev.isol8.co';
const IDEA = 'A ceramics studio that ships nationwide — landing e2e';

test.describe('Landing Funnel', () => {
  test('landing brief replays through signup with no funnel flash', async ({ page }) => {
    test.setTimeout(900_000);
    const t0 = Date.now();
    const mark = (label: string) =>
      console.log(`T+${((Date.now() - t0) / 1000).toFixed(1)}s ${label}`);
    const secretKey = process.env.CLERK_SECRET_KEY;
    if (!secretKey) throw new Error('CLERK_SECRET_KEY required');

    // ── 1. the new landing renders: pill hero + workspace demo ──
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    const idea = page.getByPlaceholder(/ceramics studio/i);
    await expect(idea).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('.ld-demo-window')).toBeVisible();
    await page.screenshot({ path: 'test-results/landing-01-hero.png' });

    // ── 2. type the idea, Start → /sign-up with the stash written ──
    await idea.fill(IDEA);
    await page.locator('form.ld-ask .ld-btn').click();
    await page.waitForURL('**/sign-up**', { timeout: 20_000 });
    mark('on /sign-up, stash check');
    const stash = await page.evaluate(() =>
      window.sessionStorage.getItem('isol8_landing_brief_v1'),
    );
    expect(stash, 'stash written before signup').toContain('ceramics');
    await page.screenshot({ path: 'test-results/landing-02-signup.png' });

    // ── 3. programmatic signup + same-tab ticket sign-in ──
    // The Clerk widget does not initialize on /sign-up under headless CI, so
    // sign in exactly the way the proven journey driver does — ticket
    // strategy from the landing page, where Clerk demonstrably loads. The
    // stash lives in sessionStorage and survives every same-tab navigation.
    const runId = `landing-${Date.now()}`;
    const email = `isol8-${runId}+clerk_test@example.com`;
    mark('creating clerk user');
    const createRes = await fetch('https://api.clerk.com/v1/users', {
      method: 'POST',
      signal: AbortSignal.timeout(45_000),
      headers: {
        Authorization: `Bearer ${secretKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        email_address: [email],
        password: `E2e-${runId}-Aa1!`,
        first_name: 'E2E',
        last_name: runId,
        skip_password_checks: true,
        unsafe_metadata: { onboarded: false, e2e_run_id: runId },
      }),
    });
    if (!createRes.ok) {
      throw new Error(`Clerk createUser ${createRes.status}: ${await createRes.text()}`);
    }
    const userId = ((await createRes.json()) as { id: string }).id;
    mark(`clerk user created ${userId}`);
    mark('back to landing for clerk');
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => (window as { Clerk?: unknown }).Clerk != null, {
      timeout: 120_000,
    });
    mark('window.Clerk exists');
    // Diagnostic: clerk-js readiness surface differs across versions
    // (loaded vs status==='ready'); log the shape so a failure explains itself.
    const shape = await page.evaluate(() => {
      const C = (window as { Clerk?: Record<string, unknown> }).Clerk;
      return {
        loaded: C?.loaded,
        status: C?.status,
        hasClient: C?.client != null,
        version: C?.version,
      };
    });
    console.log('CLERK SHAPE', JSON.stringify(shape));
    await page.waitForFunction(
      () => {
        const C = (window as { Clerk?: { loaded?: boolean; status?: string; client?: unknown } })
          .Clerk;
        return (C?.loaded === true || C?.status === 'ready') && C?.client != null;
      },
      { timeout: 60_000 },
    );
    const ticket2 = await createSignInToken({ secretKey, userId });
    await page.evaluate(async (t: string) => {
      const w = window as unknown as {
        Clerk: {
          client: {
            signIn: {
              create: (o: Record<string, string>) => Promise<{ createdSessionId: string }>;
            };
          };
          setActive: (o: { session: string }) => Promise<void>;
        };
      };
      const si = await w.Clerk.client.signIn.create({ strategy: 'ticket', ticket: t });
      await w.Clerk.setActive({ session: si.createdSessionId });
    }, ticket2);
    mark('ticket session active');

    // ── 4. /chat: the replay hook must consume the stash; the in-chat
    // funnel ("Let's get started." / the path picker) must never render ──
    await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' });
    mark('on /chat');
    await expect
      .poll(
        async () =>
          page.evaluate(() =>
            window.sessionStorage.getItem('isol8_landing_brief_v1'),
          ),
        { timeout: 60_000, message: 'replay hook consumed the stash' },
      )
      .toBeNull();
    await expect(
      page.getByText(/start something new/i),
      'in-chat funnel path picker must not appear',
    ).toHaveCount(0);
    await page.screenshot({ path: 'test-results/landing-03-chat-no-funnel.png' });

    // ── 5. provisioning runs; Friday's kickoff eventually surfaces the plan.
    // The strongest cheap signal that the brief LANDED (drain claimed the
    // funnel) is the server no longer reporting funnel_state not_started:
    // the funnel keeps not rendering while the stepper/chat surface does.
    // Poll patiently for either the chat surface or kickoff-task text. ──
    const kickoffSignal = page
      .getByText(/name it and take the handle|first moves|kickoff/i)
      .first();
    const chatSurface = page.locator('[data-chat-ready], .cream-sidebar').first();
    await expect(kickoffSignal.or(chatSurface)).toBeVisible({ timeout: 480_000 });
    await expect(page.getByText(/start something new/i)).toHaveCount(0);
    await page.screenshot({ path: 'test-results/landing-04-provisioned.png', fullPage: true });
  });
});
