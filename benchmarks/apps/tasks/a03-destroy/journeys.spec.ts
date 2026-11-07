import { test, expect } from "@playwright/test";
import { signUp, newUser, api, url, uniq } from "../_shared/helpers";

test("customers can still be added and listed", async ({ page }) => {
  await signUp(page);
  await page.goto(url("/contacts"));
  await page.getByTestId("contact-new-button").click();
  const name = uniq("Pat ");
  await page.getByTestId("contact-form-name").fill(name);
  await page.getByTestId("contact-form-email").fill(uniq("pat") + "@example.test");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("contact-form-submit").click()]); // let the write land before navigating
  await page.goto(url("/contacts"));
  await expect(page.getByTestId("contact-row-name").filter({ hasText: name })).toHaveCount(1);
  await page.getByTestId("contact-row").filter({ hasText: name }).getByTestId("contact-row-link").click();
  await expect(page.getByTestId("contact-detail-name")).toHaveText(name);
});

test("the screens say Customers now", async ({ page }) => {
  await signUp(page);
  await page.goto(url("/contacts"));
  await expect(page.locator("body")).toContainText(/customers?/i);
  await expect(page.locator("body")).not.toContainText(/\bcontacts?\b/i);
});

test("records stay private", async ({ page, browser }) => {
  await signUp(page);
  await api(page, "POST", "/api/contacts", { name: "Mine", email: uniq("m") + "@example.test" });
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/contacts")).body).toEqual([]);
  await other.ctx.close();
});
