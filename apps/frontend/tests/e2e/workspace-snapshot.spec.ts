import { expect, test } from '@playwright/test';
import { createSignInToken } from './fixtures/clerk-admin';

/**
 * Landing Funnel — workspace snapshot. Not a test of behavior: captures the
 * REAL /chat workspace (signed in as the provisioned landing-e2e user) and
 * the landing demo section at the same viewport, so the demo can be visually
 * matched against the real thing. Screenshots land in the report artifact
 * via testInfo.attach.
 */

const BASE = process.env.BASE_URL ?? 'https://dev.isol8.co';
// The user minted by the passing landing-funnel run (container provisioned).
const SNAPSHOT_USER = process.env.SNAPSHOT_USER_ID ?? 'user_3HrD2J9sWKZPjVKYlSRibS7bkSj';

test.describe('Landing Funnel — workspace snapshot', () => {
  test.use({
    extraHTTPHeaders: {},
    viewport: { width: 1440, height: 900 },
    userAgent:
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
  });

  test('capture real /chat and the landing demo', async ({ page }, testInfo) => {
    test.setTimeout(420_000);
    const secretKey = process.env.CLERK_SECRET_KEY;
    if (!secretKey) throw new Error('CLERK_SECRET_KEY required');

    const bypass = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
    const entry = bypass
      ? `${BASE}/?x-vercel-protection-bypass=${bypass}&x-vercel-set-bypass-cookie=true`
      : BASE;
    await page.goto(entry, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(
      () => {
        const C = (window as { Clerk?: { loaded?: boolean; status?: string; client?: unknown } })
          .Clerk;
        return (C?.loaded === true || C?.status === 'ready') && C?.client != null;
      },
      { timeout: 60_000 },
    );
    const ticket = await createSignInToken({ secretKey, userId: SNAPSHOT_USER });
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
    }, ticket);

    // Real workspace: wait for the dock to be up, give panels time to settle,
    // then capture. The container may need to wake — poll patiently.
    await page.goto(`${BASE}/chat`, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.cream-sidebar').first()).toBeVisible({ timeout: 300_000 });
    await page.waitForTimeout(15_000);
    await testInfo.attach('real-chat-1440', {
      body: await page.screenshot(),
      contentType: 'image/png',
    });

    // The landing demo at the same viewport, scrolled into view.
    await page.goto(BASE, { waitUntil: 'domcontentloaded' });
    await page.locator('.ld-demo-window').scrollIntoViewIfNeeded();
    await page.waitForTimeout(1_500);
    await testInfo.attach('landing-demo-1440', {
      body: await page.screenshot(),
      contentType: 'image/png',
    });
  });
});
