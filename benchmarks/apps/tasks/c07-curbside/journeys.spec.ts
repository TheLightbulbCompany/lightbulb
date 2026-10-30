// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/curbside/checkpoint-2.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect, type Browser, type Page } from "@playwright/test";
import { newUser, api, me, url, uniq } from "../_shared/helpers";

// A merchant with a restaurant + menu item, a customer, and an order that is "placed".
async function scene(browser: Browser) {
  const merchant = await newUser(browser);
  const rest = (await api(merchant.page, "POST", "/api/restaurants", { name: uniq("Diner "), cuisine: "US", address: "1 Main" })).body;
  const item = (await api(merchant.page, "POST", "/api/restaurants/" + rest.id + "/menu-items", { name: "Burger", description: "Beef", priceCents: 900 })).body;
  const customer = await newUser(browser);
  const order = (await api(customer.page, "POST", "/api/orders", { restaurantId: rest.id, items: [{ menuItemId: item.id, quantity: 1 }] })).body;
  return { merchant, customer, order };
}
const go = (page: Page, id: string, to: string) => api(page, "POST", "/api/orders/" + id + "/transition", { to });
async function becomeCourier(browser: Browser) {
  const c = await newUser(browser);
  await c.page.goto(url("/courier"));
  await c.page.getByTestId("courier-register-button").click();
  await expect.poll(async () => (await me(c.page)).isCourier).toBe(true);
  return c;
}

test("merchant flag, and only the owner sees the queue", async ({ browser }) => {
  const s = await scene(browser);
  const m = await me(s.merchant.page);
  expect(m.isMerchant).toBe(true);
  expect(m.restaurantIds).toHaveLength(1);
  expect((await me(s.customer.page)).isMerchant).toBe(false);
  expect((await api(s.merchant.page, "GET", "/api/merchant/orders")).body.map((o: any) => o.id)).toContain(s.order.id);
  expect((await api(s.customer.page, "GET", "/api/merchant/orders")).status).toBe(403);
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/merchant/orders")).status).toBe(403);
  await s.merchant.ctx.close(); await s.customer.ctx.close(); await other.ctx.close();
});

test("the whole delivery, step by step", async ({ browser }) => {
  const s = await scene(browser);
  const courier = await becomeCourier(browser);
  const mp = s.merchant.page;
  await mp.goto(url("/merchant/orders"));
  const row = mp.locator('[data-testid="merchant-order-row"][data-order-id="' + s.order.id + '"]');
  const button = { accepted: "transition-accepted", preparing: "transition-preparing", ready: "transition-ready" } as const;
  for (const step of ["accepted", "preparing", "ready"] as const) {
    await row.getByTestId(button[step]).click();
    await expect(row.getByTestId("merchant-order-status")).toHaveText(step);
  }
  const cp = courier.page;
  await cp.goto(url("/courier"));
  await Promise.all([cp.waitForResponse((r) => r.request().method() !== "GET"), cp.locator('[data-testid="available-row"][data-order-id="' + s.order.id + '"]').getByTestId("claim-button").click()]); // let the write land before navigating
  await cp.goto(url("/courier/deliveries"));
  const mine = cp.locator('[data-testid="my-delivery-row"][data-order-id="' + s.order.id + '"]');
  await mine.getByTestId("transition-picked_up").click();
  await expect(mine.getByTestId("my-delivery-status")).toHaveText("picked_up");
  await mine.getByTestId("transition-delivered").click();
  await expect(mine.getByTestId("my-delivery-status")).toHaveText("delivered");
  await s.customer.page.goto(url("/orders"));
  await expect(s.customer.page.getByTestId("order-row").first().getByTestId("order-row-status")).toHaveText("delivered");
  await s.merchant.ctx.close(); await s.customer.ctx.close(); await courier.ctx.close();
});

test("wrong actor is 403, illegal step is 409, and nothing changes", async ({ browser }) => {
  const s = await scene(browser);
  expect((await go(s.customer.page, s.order.id, "accepted")).status).toBe(403);
  expect((await go(s.merchant.page, s.order.id, "ready")).status).toBe(409);
  expect((await go(s.merchant.page, s.order.id, "delivered")).status).toBe(409);
  expect((await api(s.customer.page, "GET", "/api/orders/" + s.order.id)).body.status).toBe("placed");
  expect((await go(s.customer.page, s.order.id, "cancelled")).status).toBeLessThan(300);
  expect((await go(s.merchant.page, s.order.id, "accepted")).status).toBe(409);
  await s.merchant.ctx.close(); await s.customer.ctx.close();
});

test("two couriers race for one delivery: exactly one wins", async ({ browser }) => {
  const s = await scene(browser);
  for (const to of ["accepted", "preparing", "ready"]) expect((await go(s.merchant.page, s.order.id, to)).status).toBeLessThan(300);
  const a = await becomeCourier(browser), b = await becomeCourier(browser);
  expect((await api(s.customer.page, "GET", "/api/courier/deliveries/available")).status).toBe(403);
  const res = await Promise.all([a, b].map((c) => api(c.page, "POST", "/api/deliveries/" + s.order.id + "/claim")));
  expect(res.filter((r) => r.status < 300)).toHaveLength(1);
  expect(res.filter((r) => r.status === 409)).toHaveLength(1);
  const winner = res.findIndex((r) => r.status < 300) === 0 ? a : b;
  const loser = winner === a ? b : a;
  expect((await api(s.merchant.page, "GET", "/api/orders/" + s.order.id)).body.courierId).toBe(winner.id);
  expect((await go(loser.page, s.order.id, "picked_up")).status).toBe(403);
  await s.merchant.ctx.close(); await s.customer.ctx.close(); await a.ctx.close(); await b.ctx.close();
});
