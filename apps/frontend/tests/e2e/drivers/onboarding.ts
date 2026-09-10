import type { Page } from '@playwright/test';

/** Create the workspace and record its cleanup handle before waiting for chat. */
export async function onboardWorkspace(
  page: Page,
  orgName: string,
  onOrgCreated: (orgId: string) => void,
): Promise<{ orgId: string }> {
  await page.goto('/chat');
  await page.waitForURL(
    /\/(onboarding|session-tasks\/choose-organization)/,
    { timeout: 30_000 },
  );
  const nameInput = page.getByRole('textbox', { name: /name/i });
  if (!(await nameInput.isVisible())) {
    await page
      .getByRole('button', { name: /create (a )?workspace/i })
      .first()
      .click();
    await nameInput.waitFor({ state: 'visible', timeout: 30_000 });
  }
  await nameInput.fill(orgName);
  await page.getByRole('button', { name: /create|next/i }).first().click();

  // Wait for Clerk to register the new org BEFORE waiting for any URL —
  // the org is the load-bearing teardown handle. Hand it to the caller
  // immediately so even a downstream throw still leaves cleanupUser able
  // to delete it.
  await page.waitForFunction(
    () => {
      const w = window as Window & {
        Clerk?: { organization?: { id?: string } };
      };
      return Boolean(w.Clerk?.organization?.id);
    },
    undefined,
    { timeout: 60_000 },
  );
  const orgId = await page.evaluate(() => {
    const w = window as Window & {
      Clerk?: { organization?: { id?: string } };
    };
    return w.Clerk!.organization!.id!;
  });
  onOrgCreated(orgId);

  // Now wait for the /chat redirect. If this throws, orgId is already
  // recorded so cleanup will still delete it.
  await page.waitForURL(/\/chat/, { timeout: 60_000 });

  return { orgId };
}

/** Accept the invited workspace before any create-workspace form is shown. */
export async function acceptWorkspaceInvitation(
  page: Page,
  workspaceName: string,
): Promise<{ orgId: string }> {
  await page.goto('/chat');
  await page.waitForURL(
    /\/(onboarding|session-tasks\/choose-organization)/,
    { timeout: 30_000 },
  );

  await page.getByText(workspaceName, { exact: true }).waitFor({
    state: 'visible',
    timeout: 30_000,
  });
  const createForm = page.getByRole('heading', {
    name: /^create (a |your )?workspace$/i,
  });
  if (await createForm.isVisible()) {
    throw new Error('Workspace creation was shown before the pending invitation');
  }

  await page.getByRole('button', { name: /^accept/i }).first().click();
  await page.waitForFunction(
    () => {
      const w = window as Window & {
        Clerk?: { organization?: { id?: string } };
      };
      return Boolean(w.Clerk?.organization?.id);
    },
    undefined,
    { timeout: 60_000 },
  );
  const orgId = await page.evaluate(() => {
    const w = window as Window & {
      Clerk?: { organization?: { id?: string } };
    };
    return w.Clerk!.organization!.id!;
  });
  await page.waitForURL(/\/chat/, { timeout: 60_000 });

  return { orgId };
}
