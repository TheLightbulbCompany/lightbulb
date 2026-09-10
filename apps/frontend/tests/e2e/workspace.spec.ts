import { test, expect } from '@playwright/test';
import { clerkSetup, setupClerkTestingToken } from '@clerk/testing/playwright';
import { createE2EUser, cleanupUser, type E2EUser } from './fixtures/user';
import { signIn } from './drivers/sign-in';
import { onboardWorkspace } from './drivers/onboarding';
import {
  waitForChatReady,
  sendMessageAndWaitForResponse,
} from './drivers/chat';
import { containerHealthy } from './assertions/container';

test.describe('E2E: Workspace trial happy path', () => {
  test.describe.configure({ mode: 'serial' });
  // Allow for a cold workspace start before its first chat.
  test.setTimeout(35 * 60_000);

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
    user = await createE2EUser(page);
    console.log(
      `[e2e] runId=${user.runId} clerkUserId=${user.clerkUserId} email=${user.email}`,
    );
  });

  test.afterAll(async () => {
    if (user) await cleanupUser(user);
  });

  test('Step 1: sign in', async () => {
    if (!process.env.BASE_URL) throw new Error('BASE_URL is required');
    await signIn(user.page, process.env.BASE_URL, user.clerkUserId);
    expect(user.page.url()).not.toContain('/sign-in');
  });

  test('Step 2: create and activate a workspace', async () => {
    // Record orgId on `user` the instant Clerk reports it, BEFORE the
    // /chat redirect wait — otherwise a downstream UI failure here would
    // leak the org because afterAll's cleanupUser would skip deleteOrg
    // (Codex P1 on PR #309).
    await onboardWorkspace(user.page, `e2e-workspace-${user.runId}`, (orgId) => {
      user.orgId = orgId;
    });
    expect(user.page.url()).toContain('/chat');
  });

  test('Step 3: workspace trial chat', async () => {
    await containerHealthy(user.api, { timeoutMs: 10 * 60_000 });
    await waitForChatReady(user.page);
    await sendMessageAndWaitForResponse(user.page, 'Say "hello" and nothing else.');
  });

});
