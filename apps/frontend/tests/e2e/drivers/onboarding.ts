import type { Page } from '@playwright/test';

/** Create the workspace and record its cleanup handle before waiting for chat. */
export async function onboardWorkspace(
  page: Page,
  orgName: string,
  onOrgCreated: (orgId: string) => void,
): Promise<{ orgId: string }> {
  await page.goto('/chat');
  await page.waitForURL(/\/onboarding/, { timeout: 30_000 });
  await page.getByRole('textbox', { name: /name/i }).fill(orgName);
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
