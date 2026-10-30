// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/relay-crm/checkpoint-1.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, url } from "../_shared/helpers";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CSV = path.join(path.dirname(fileURLToPath(import.meta.url)), "attachments", "contacts.csv");

async function importCsv(page: import("@playwright/test").Page) {
  await page.goto(url("/contacts/import"));
  await page.getByTestId("import-file").setInputFiles(CSV);
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("import-submit").click()]); // let the import land before the next step
}

test("import the sample CSV", async ({ page }) => {
  await signUp(page);
  await importCsv(page);
  await expect(page.getByTestId("import-result")).toContainText("4");
  await page.goto(url("/contacts"));
  await expect(page.getByTestId("contact-row")).toHaveCount(4);
  await expect(page.getByTestId("contact-row-name").filter({ hasText: "Ada Lovelace" })).toHaveCount(1);
  await expect(page.getByTestId("contact-row").filter({ hasText: "Grace" })).toContainText("Navy Labs");
  await expect(page.getByTestId("contact-row-name").filter({ hasText: "nobody" })).toHaveCount(0);
});

test("shared companies are created once and importing twice does not duplicate", async ({ page }) => {
  await signUp(page);
  await importCsv(page);
  await importCsv(page);
  const companies = (await api(page, "GET", "/api/companies")).body;
  expect(companies.length).toBe(3);
  expect((await api(page, "GET", "/api/contacts")).body.length).toBe(4);
  await page.goto(url("/companies"));
  await page.getByTestId("company-row").filter({ hasText: "Analytical Engines" }).click();
  await expect(page.getByTestId("company-detail-name")).toHaveText("Analytical Engines");
  await expect(page.getByTestId("company-contact-row")).toHaveCount(2);
});

test("search filters imported contacts", async ({ page }) => {
  await signUp(page);
  await importCsv(page);
  await page.goto(url("/contacts"));
  await page.getByTestId("contacts-search").fill("turing");
  await expect(page.getByTestId("contact-row")).toHaveCount(1);
  await page.getByTestId("contacts-search").fill("analytical.test");
  await expect(page.getByTestId("contact-row")).toHaveCount(2);
});

test("imported records stay private", async ({ page, browser }) => {
  await signUp(page);
  await importCsv(page);
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/contacts")).body).toEqual([]);
  expect((await api(other.page, "GET", "/api/companies")).body).toEqual([]);
  await other.page.goto(url("/contacts"));
  await expect(other.page.getByTestId("contacts-empty")).toBeVisible();
  await other.ctx.close();
});
