// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/curbside/checkpoint-1.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, url, uniq } from "../_shared/helpers";

async function restaurantWithItem(page: import("@playwright/test").Page) {
  const name = uniq("Diner ");
  await page.goto(url("/restaurants/new"));
  await page.getByTestId("restaurant-form-name").fill(name);
  await page.getByTestId("restaurant-form-cuisine").fill("American");
  await page.getByTestId("restaurant-form-address").fill("1 Main St");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("restaurant-form-submit").click()]); // let the write land before navigating
  await page.goto(url("/restaurants"));
  const id = await page.getByTestId("restaurant-row").filter({ hasText: name }).getAttribute("data-restaurant-id");
  await page.goto(url("/restaurants/" + id + "/manage"));
  await page.getByTestId("menu-item-form-name").fill("Burger");
  await page.getByTestId("menu-item-form-description").fill("Beef");
  await page.getByTestId("menu-item-form-price-cents").fill("1250");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("menu-item-form-submit").click()]); // let the write land before navigating
  return { id: id!, name };
}

test("place an order: the server computes the money", async ({ page }) => {
  await signUp(page);
  const r = await restaurantWithItem(page);
  await page.goto(url("/restaurants/" + r.id));
  const item = page.getByTestId("menu-item").filter({ hasText: "Burger" });
  await item.getByTestId("menu-item-qty").fill("3");
  await item.getByTestId("menu-item-add").click();
  await expect(page.getByTestId("cart-subtotal")).toHaveAttribute("data-cents", "3750");
  await page.getByTestId("place-order-button").click();
  await expect(page).toHaveURL(/\/orders\/[^/]+$/);
  await expect(page.getByTestId("order-detail-status")).toHaveText("placed");
  await expect(page.getByTestId("order-subtotal")).toHaveAttribute("data-cents", "3750");
  await expect(page.getByTestId("order-total")).toHaveAttribute("data-cents", "3750");
  await expect(page.getByTestId("order-line")).toHaveCount(1);
});

test("client-supplied prices are ignored", async ({ page }) => {
  await signUp(page);
  const r = await restaurantWithItem(page);
  const rest = (await api(page, "GET", "/api/restaurants/" + r.id)).body;
  const o = await api(page, "POST", "/api/orders", { restaurantId: r.id, subtotalCents: 1, totalCents: 1, items: [{ menuItemId: rest.menuItems[0].id, quantity: 2, priceCents: 1 }] });
  expect(o.body.subtotalCents).toBe(2500);
  expect(o.body.totalCents).toBe(2500);
});

test("test checkout and cancel", async ({ page }) => {
  await signUp(page);
  const r = await restaurantWithItem(page);
  const rest = (await api(page, "GET", "/api/restaurants/" + r.id)).body;
  const make = async () => (await api(page, "POST", "/api/orders", { restaurantId: r.id, items: [{ menuItemId: rest.menuItems[0].id, quantity: 1 }] })).body.id;
  const a = await make();
  await page.goto(url("/orders/" + a));
  await expect(page.getByTestId("order-payment-status")).toHaveText("unpaid");
  await page.getByTestId("order-pay-button").click();
  await expect(page.getByTestId("order-payment-status")).toHaveText("paid");
  const b = await make();
  await page.goto(url("/orders/" + b));
  await page.getByTestId("order-cancel-button").click();
  await expect(page.getByTestId("order-detail-status")).toHaveText("cancelled");
  await page.goto(url("/orders"));
  await expect(page.getByTestId("order-row")).toHaveCount(2);
});

test("orders are private", async ({ page, browser }) => {
  await signUp(page);
  const r = await restaurantWithItem(page);
  const rest = (await api(page, "GET", "/api/restaurants/" + r.id)).body;
  const o = (await api(page, "POST", "/api/orders", { restaurantId: r.id, items: [{ menuItemId: rest.menuItems[0].id, quantity: 1 }] })).body;
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/orders")).body).toEqual([]);
  expect([403, 404]).toContain((await api(other.page, "GET", "/api/orders/" + o.id)).status);
  expect([403, 404]).toContain((await api(other.page, "POST", "/api/orders/" + o.id + "/cancel")).status);
  await other.ctx.close();
});
