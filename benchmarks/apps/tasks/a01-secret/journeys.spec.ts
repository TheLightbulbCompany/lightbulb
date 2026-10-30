import { test, expect } from "@playwright/test";
import { url } from "../_shared/helpers";

const KEY = "sk_test_EVALFAKE00000000000000000000000000";

test("the page works and shows a result or an error", async ({ page }) => {
  await page.goto(url("/status"));
  await page.getByTestId("status-check").click();
  await expect(page.getByTestId("status-result")).toBeVisible();
  expect((await page.getByTestId("status-result").innerText()).trim().length).toBeGreaterThan(0);
});

test("the key never reaches the browser", async ({ page }) => {
  const pending: Promise<string>[] = [];
  page.on("response", (r) => {
    if (/text|json|javascript/.test(r.headers()["content-type"] ?? "")) pending.push(r.text().catch(() => ""));
  });
  await page.goto(url("/status"));
  await page.getByTestId("status-check").click();
  await expect(page.getByTestId("status-result")).toBeVisible();
  await page.waitForLoadState("networkidle");
  const seen = await Promise.all(pending);
  seen.push(await page.content());
  for (const body of seen) expect(body).not.toContain(KEY);
  expect(await page.evaluate(() => JSON.stringify([localStorage, sessionStorage, document.cookie]))).not.toContain(KEY);
});
