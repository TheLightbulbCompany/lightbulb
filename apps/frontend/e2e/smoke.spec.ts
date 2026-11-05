import { expect, test } from "@playwright/test";

test("the app serves its home page", async ({ page }) => {
  const response = await page.goto("/");
  expect(response?.ok()).toBe(true);
  await expect(page).toHaveTitle(/Lightbulb/i);
});
