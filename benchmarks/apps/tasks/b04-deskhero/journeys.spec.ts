// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/deskhero/checkpoint-1.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, url, uniq } from "../_shared/helpers";

test("create a ticket and see it in the list", async ({ page }) => {
  await signUp(page);
  await expect(page).toHaveURL(/\/tickets$/);
  await expect(page.getByTestId("ticket-empty")).toBeVisible();
  const subject = uniq("Printer jam ");
  await page.getByTestId("new-ticket-link").click();
  await page.getByTestId("ticket-subject").fill(subject);
  await page.getByTestId("ticket-body").fill("Third floor printer is jammed");
  await page.getByTestId("ticket-priority").selectOption("high");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("ticket-submit").click()]); // let the write land before navigating
  await page.goto(url("/tickets"));
  const row = page.getByTestId("ticket-row").filter({ hasText: subject });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("high");
  await expect(row).toContainText("open");
});

test("an empty subject creates nothing", async ({ page }) => {
  await signUp(page);
  await page.goto(url("/tickets/new"));
  await page.getByTestId("ticket-submit").click();
  await expect(page.getByTestId("ticket-error")).toBeVisible();
  expect((await api(page, "GET", "/api/tickets")).body).toEqual([]);
});

test("edit, close, reopen, delete", async ({ page }) => {
  await signUp(page);
  const t = (await api(page, "POST", "/api/tickets", { subject: "VPN down", body: "x", priority: "low" })).body;
  await page.goto(url("/tickets/" + t.id));
  await expect(page.getByTestId("ticket-detail-status")).toHaveText("open");
  await page.getByTestId("ticket-edit").click();
  await page.getByTestId("ticket-subject").fill("VPN still down");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("ticket-submit").click()]); // let the write land before navigating
  await page.goto(url("/tickets/" + t.id));
  await expect(page.getByTestId("ticket-detail-subject")).toHaveText("VPN still down");
  await page.getByTestId("ticket-close").click();
  await expect(page.getByTestId("ticket-detail-status")).toHaveText("closed");
  await page.getByTestId("ticket-reopen").click();
  await expect(page.getByTestId("ticket-detail-status")).toHaveText("open");
  await Promise.all([page.waitForResponse((r) => r.request().method() === "DELETE"), page.getByTestId("ticket-delete").click()]); // let the delete land before asking for the ticket
  expect((await api(page, "GET", "/api/tickets/" + t.id)).status).toBe(404);
});

test("another user's ticket is a 404 and the API needs a session", async ({ page, browser }) => {
  await signUp(page);
  const t = (await api(page, "POST", "/api/tickets", { subject: "Mine", body: "", priority: "low" })).body;
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/tickets/" + t.id)).status).toBe(404);
  expect((await api(other.page, "PATCH", "/api/tickets/" + t.id, { subject: "x" })).status).toBe(404);
  expect((await api(other.page, "DELETE", "/api/tickets/" + t.id)).status).toBe(404);
  expect((await api(other.page, "GET", "/api/tickets")).body).toEqual([]);
  await other.ctx.close();
  const anon = await browser.newContext();
  expect((await anon.request.get(url("/api/tickets"))).status()).toBe(401);
  expect((await anon.request.get(url("/api/me"))).status()).toBe(401);
  await anon.close();
});
