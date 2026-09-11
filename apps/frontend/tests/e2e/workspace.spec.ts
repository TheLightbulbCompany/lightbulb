import { test, expect } from '@playwright/test';
import { clerkSetup, setupClerkTestingToken } from '@clerk/testing/playwright';
import { createE2EUser, cleanupUser, type E2EUser } from './fixtures/user';
import { createOrganizationInvitation } from './fixtures/clerk-admin';
import { signIn } from './drivers/sign-in';
import {
  acceptWorkspaceInvitation,
  onboardWorkspace,
} from './drivers/onboarding';
import { containerHealthy } from './assertions/container';

async function organizationIdForNextStatusRequest(
  user: E2EUser,
  expectedOrgId: string,
): Promise<string> {
  const token = await user.page.evaluate(async (organizationId) => {
    const w = window as Window & {
      Clerk?: {
        session?: {
          getToken: (options: {
            organizationId: string;
            skipCache: boolean;
          }) => Promise<string | null>;
        };
      };
    };
    return w.Clerk?.session?.getToken({ organizationId, skipCache: true });
  }, expectedOrgId);
  if (!token) throw new Error('No Clerk token available for /container/status');

  const payload = JSON.parse(
    Buffer.from(token.split('.')[1], 'base64url').toString('utf8'),
  ) as { o?: { id?: unknown } };
  if (typeof payload.o?.id !== 'string') {
    throw new Error('Clerk token for /container/status had no organization ID');
  }
  const apiUrl = (
    process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000/api/v1'
  ).replace(/\/$/, '');
  const response = await fetch(`${apiUrl}/container/status`, {
    headers: {
      Authorization: `Bearer ${token}`,
      'X-E2E-Run-Id': user.runId,
    },
  });
  if (!response.ok) {
    throw new Error(`/container/status returned ${response.status}`);
  }
  return payload.o.id;
}

test.describe('E2E: Workspace invitation and trial happy path', () => {
  test.describe.configure({ mode: 'serial' });
  // Allow for a cold workspace start before its first chat.
  test.setTimeout(35 * 60_000);

  let inviter: E2EUser;
  let invitee: E2EUser;
  const statusOwners = new Set<string>();

  test.beforeAll(async ({ browser }) => {
    await clerkSetup();
    async function createUser() {
      const ctx = await browser.newContext({
        extraHTTPHeaders: process.env.VERCEL_AUTOMATION_BYPASS_SECRET
          ? { 'x-vercel-protection-bypass': process.env.VERCEL_AUTOMATION_BYPASS_SECRET }
          : {},
      });
      const page = await ctx.newPage();
      await setupClerkTestingToken({ page });
      return createE2EUser(page);
    }
    inviter = await createUser();
    invitee = await createUser();
    console.log(
      `[e2e] inviterRunId=${inviter.runId} inviteeRunId=${invitee.runId}`,
    );
  });

  test.afterAll(async () => {
    const failures: unknown[] = [];
    if (invitee) {
      try {
        await cleanupUser(invitee, { cleanupWorkspace: false });
      } catch (error) {
        failures.push(error);
      }
    }
    if (inviter) {
      try {
        await cleanupUser(inviter);
      } catch (error) {
        failures.push(error);
      }
    }
    if (failures.length) {
      throw new Error(
        `Workspace E2E cleanup failed:\n${failures.map(String).join('\n')}`,
      );
    }
  });

  test('Step 1: sign in the inviter', async () => {
    if (!process.env.BASE_URL) throw new Error('BASE_URL is required');
    await signIn(inviter.page, process.env.BASE_URL, inviter.clerkUserId);
    expect(inviter.page.url()).not.toContain('/sign-in');
  });

  test('Step 2: create and activate the shared workspace', async () => {
    // Record orgId on `user` the instant Clerk reports it, BEFORE the
    // /chat redirect wait — otherwise a downstream UI failure here would
    // leak the org because afterAll's cleanupUser would skip deleteOrg
    // (Codex P1 on PR #309).
    await onboardWorkspace(inviter.page, `e2e-workspace-${inviter.runId}`, (orgId) => {
      inviter.orgId = orgId;
    });
    expect(inviter.page.url()).toContain('/chat');
  });

  test('Step 3: invitee joins with native invitation precedence over creation', async () => {
    if (!process.env.BASE_URL) throw new Error('BASE_URL is required');
    const clerkKey = process.env.CLERK_SECRET_KEY;
    if (!clerkKey) throw new Error('CLERK_SECRET_KEY is required');
    if (!inviter.orgId) throw new Error('Inviter workspace was not created');

    await createOrganizationInvitation({
      secretKey: clerkKey,
      orgId: inviter.orgId,
      inviterUserId: inviter.clerkUserId,
      email: invitee.email,
    });
    await signIn(invitee.page, process.env.BASE_URL, invitee.clerkUserId);
    const { orgId } = await acceptWorkspaceInvitation(
      invitee.page,
      `e2e-workspace-${inviter.runId}`,
    );
    invitee.orgId = orgId;
    expect(orgId).toBe(inviter.orgId);
  });

  test('Step 4: both members use one status owner and one workspace trial', async () => {
    if (!inviter.orgId) throw new Error('Inviter workspace was not created');
    await containerHealthy(inviter.api, { timeoutMs: 10 * 60_000 });

    statusOwners.add(
      await organizationIdForNextStatusRequest(inviter, inviter.orgId),
    );
    statusOwners.add(
      await organizationIdForNextStatusRequest(invitee, inviter.orgId),
    );
    expect([...statusOwners]).toEqual([inviter.orgId]);

    const page = inviter.page;
    const workspace = page.getByRole('navigation', { name: 'Workspace', exact: true });
    await expect(workspace).toBeVisible();
    for (const name of ['Home', 'Activity', 'Tasks', 'Browser']) {
      await expect(workspace.getByRole('button', { name, exact: true })).toBeVisible();
    }
    const conversations = page.getByRole('navigation', { name: 'Conversations' });
    await expect(conversations).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText('New session', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Reset workspace layout' })).toHaveCount(0);

    const general = conversations.locator('.room-directory-entry').filter({ hasText: 'general' });
    await expect(general).toHaveCount(1);
    await general.click({ timeout: 30_000 });
    const messages = page.getByRole('region', { name: 'Messages', exact: true });
    await expect(messages).toBeVisible();
    await expect(messages.getByText(
      'Your team is ready: Engineering, Finance, Growth, Analytics, and Customer Operations. I’ll coordinate. Tell us what you’re trying to build or change.',
      { exact: true },
    )).toHaveCount(1);
    const responseModes = page.getByRole('combobox', { name: /^Response for / });
    await expect(responseModes).toHaveCount(6);
    expect(await responseModes.evaluateAll((selects) =>
      selects.map((select) => (select as HTMLSelectElement).value),
    )).toEqual(['active', 'mentions', 'mentions', 'mentions', 'mentions', 'mentions']);
    await page.getByRole('textbox', { name: 'Message', exact: true }).fill('Workspace UI smoke');
    await page.getByRole('button', { name: 'Send message', exact: true }).click();
    const sent = messages.getByRole('article', { name: /^Message from / })
      .filter({ hasText: 'Workspace UI smoke' }).first();
    const reply = sent.getByRole('button', { name: 'Reply', exact: true });
    await reply.click({ timeout: 30_000 });
    const thread = page.getByRole('complementary', { name: 'Thread', exact: true });
    await expect(thread).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(thread).toHaveCount(0);
    await expect(reply).toBeFocused();
  });
});
