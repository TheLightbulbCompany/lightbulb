// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/slotline/checkpoint-1.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, url, uniq } from "../_shared/helpers";

async function seed(page: import("@playwright/test").Page) {
  const pn = uniq("Dr "), sn = uniq("Checkup ");
  await page.goto(url("/practitioners/new"));
  await page.getByTestId("practitioner-form-name").fill(pn);
  await page.getByTestId("practitioner-form-specialty").fill("GP");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("practitioner-form-submit").click()]); // let the write land before navigating
  await page.goto(url("/services/new"));
  await page.getByTestId("service-form-name").fill(sn);
  await page.getByTestId("service-form-duration").fill("30");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("service-form-submit").click()]); // let the write land before navigating
  return { pn, sn };
}

test("header shows the clinic timezone and lists are shared", async ({ page }) => {
  await signUp(page);
  await expect(page.getByTestId("clinic-timezone")).toHaveText("America/Denver");
  const { pn, sn } = await seed(page);
  await page.goto(url("/practitioners"));
  await expect(page.getByTestId("practitioner-row").filter({ hasText: pn })).toHaveCount(1);
  await page.goto(url("/services"));
  await expect(page.getByTestId("service-row").filter({ hasText: sn })).toHaveCount(1);
});

test("a booking is stored in UTC with a server-computed end", async ({ page }) => {
  await signUp(page);
  const { pn, sn } = await seed(page);
  await page.goto(url("/bookings/new"));
  await page.getByTestId("booking-form-practitioner").selectOption({ label: pn });
  await page.getByTestId("booking-form-service").selectOption({ label: sn });
  await page.getByTestId("booking-form-start").fill("2030-01-15T10:00");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("booking-form-submit").click()]); // let the write land before navigating
  await page.goto(url("/bookings"));
  const row = page.getByTestId("booking-row").first();
  await expect(row).toHaveAttribute("data-start", "2030-01-15T17:00:00.000Z");
  await expect(row).toHaveAttribute("data-end", "2030-01-15T17:30:00.000Z");
  await expect(row).toHaveAttribute("data-status", "booked");
});

test("cancelling keeps the booking", async ({ page }) => {
  await signUp(page);
  const { pn, sn } = await seed(page);
  await page.goto(url("/bookings/new"));
  await page.getByTestId("booking-form-practitioner").selectOption({ label: pn });
  await page.getByTestId("booking-form-service").selectOption({ label: sn });
  await page.getByTestId("booking-form-start").fill("2030-01-16T09:00");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("booking-form-submit").click()]); // let the write land before navigating
  await page.goto(url("/bookings"));
  await page.getByTestId("booking-row-link").first().click();
  await page.getByTestId("booking-cancel-button").click();
  await page.getByTestId("booking-cancel-confirm").click();
  await expect(page.getByTestId("booking-detail-status")).toHaveText("cancelled");
  await page.goto(url("/bookings"));
  await expect(page.getByTestId("booking-row")).toHaveCount(1);
  await expect(page.getByTestId("booking-row-status").first()).toHaveText("cancelled");
});

test("bookings are private", async ({ page, browser }) => {
  await signUp(page);
  const { pn, sn } = await seed(page);
  await page.goto(url("/bookings/new"));
  await page.getByTestId("booking-form-practitioner").selectOption({ label: pn });
  await page.getByTestId("booking-form-service").selectOption({ label: sn });
  await page.getByTestId("booking-form-start").fill("2030-01-17T09:00");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("booking-form-submit").click()]); // let the write land before listing
  const mine = (await api(page, "GET", "/api/bookings")).body;
  expect(mine.length).toBe(1);
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/bookings")).body).toEqual([]);
  expect([403, 404]).toContain((await api(other.page, "GET", "/api/bookings/" + mine[0].id)).status);
  await other.page.goto(url("/bookings"));
  await expect(other.page.getByTestId("bookings-empty")).toBeVisible();
  await other.ctx.close();
});
