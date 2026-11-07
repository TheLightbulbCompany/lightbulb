import { test, expect } from "@playwright/test";
import { signUp, api, url, uniq } from "../_shared/helpers";

const AUTH = { authorization: "Bearer " + (process.env.CRON_SECRET ?? "eval-cron-secret") };

test("add items and see the pending count", async ({ page }) => {
  await signUp(page);
  await page.goto(url("/items"));
  for (const t of [uniq("Read "), uniq("Try ")]) {
    await page.getByTestId("item-title").fill(t);
    await page.getByTestId("item-add").click();
    await expect(page.getByTestId("item-row").filter({ hasText: t })).toHaveCount(1);
  }
  await expect(page.getByTestId("items-pending-count")).toContainText("2");
});

test("the job refuses callers without the secret", async ({ request }) => {
  expect((await request.get(url("/api/cron/digest"))).status()).toBe(401);
  expect((await request.get(url("/api/cron/digest?dryRun=1"), { headers: { authorization: "Bearer wrong" } })).status()).toBe(401);
});

test("dry run reports and changes nothing; a real run sends once", async ({ page, request }) => {
  const c = await signUp(page);
  await page.goto(url("/items"));
  for (const t of [uniq("one "), uniq("two ")]) {
    await page.getByTestId("item-title").fill(t);
    await page.getByTestId("item-add").click();
    await expect(page.getByTestId("item-row").filter({ hasText: t })).toHaveCount(1); // let the add land before the next one
  }
  await expect(page.getByTestId("item-row")).toHaveCount(2);
  const dry = await request.get(url("/api/cron/digest?dryRun=1"), { headers: AUTH });
  expect(dry.status()).toBe(200);
  const plan = await dry.json();
  expect(plan.dryRun).toBe(true);
  expect(plan.digests).toContainEqual({ email: c.email, itemCount: 2 });
  await page.goto(url("/digests"));
  await expect(page.getByTestId("digest-row")).toHaveCount(0);
  await page.goto(url("/items"));
  await expect(page.getByTestId("items-pending-count")).toContainText("2");

  const real = await request.get(url("/api/cron/digest"), { headers: AUTH });
  expect(real.status()).toBe(200);
  expect((await real.json()).dryRun).toBe(false);
  await page.goto(url("/digests"));
  await expect(page.getByTestId("digest-row")).toHaveCount(1);
  await expect(page.getByTestId("digest-item-count")).toHaveText("2");
  await page.goto(url("/items"));
  await expect(page.getByTestId("items-pending-count")).toContainText("0");

  await request.get(url("/api/cron/digest"), { headers: AUTH });
  await page.goto(url("/digests"));
  await expect(page.getByTestId("digest-row")).toHaveCount(1);
  expect((await api(page, "GET", "/api/items")).body.length).toBe(2);
});
