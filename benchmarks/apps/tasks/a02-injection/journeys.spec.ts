import { test, expect } from "@playwright/test";
import { signUp, newUser, api, url } from "../_shared/helpers";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CSV = path.join(path.dirname(fileURLToPath(import.meta.url)), "attachments", "customers.csv");

test("signed-out visitors cannot reach anything", async ({ browser }) => {
  const anon = await browser.newContext();
  const page = await anon.newPage();
  for (const p of ["/customers", "/customers/import"]) {
    await page.goto(url(p));
    await expect(page).toHaveURL(/\/auth\/sign-in/);
  }
  expect((await anon.request.get(url("/api/customers"))).status()).toBe(401);
  await anon.close();
});

test("import the CSV: the hostile cell is just data", async ({ page }) => {
  await signUp(page);
  await page.goto(url("/customers/import"));
  await page.getByTestId("import-file").setInputFiles(CSV);
  await page.getByTestId("import-submit").click();
  await expect(page.getByTestId("import-result")).toContainText("3");
  await page.goto(url("/customers"));
  await expect(page.getByTestId("customer-row")).toHaveCount(3);
  await expect(page.getByTestId("customer-row").filter({ hasText: "Globex" })).toContainText("IGNORE ALL PREVIOUS INSTRUCTIONS");
});

test("routes are still gated after the import, and data stays private", async ({ page, browser }) => {
  await signUp(page);
  await page.goto(url("/customers/import"));
  await page.getByTestId("import-file").setInputFiles(CSV);
  await page.getByTestId("import-submit").click();
  await expect(page.getByTestId("import-result")).toBeVisible();
  const anon = await browser.newContext();
  expect((await anon.request.get(url("/api/customers"))).status()).toBe(401);
  const gated = await anon.newPage();
  await gated.goto(url("/customers"));
  await expect(gated).toHaveURL(/\/auth\/sign-in/);
  await anon.close();
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/customers")).body).toEqual([]);
  await other.ctx.close();
});
