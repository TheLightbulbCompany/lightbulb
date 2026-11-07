// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/deskhero/checkpoint-2.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, me, url, uniq } from "../_shared/helpers";

const adminEmail = () => "admin+" + uniq("a") + "@example.test";

test("requesters stay out of staff areas", async ({ browser }) => {
  const r = await newUser(browser);
  await expect(r.page.getByTestId("role-badge")).toHaveText("requester");
  await expect(r.page).toHaveURL(/\/tickets/);
  await r.page.goto(url("/admin"));
  await expect(r.page.getByTestId("admin-dashboard")).toHaveCount(0);
  await r.page.goto(url("/agent"));
  await expect(r.page.getByTestId("agent-dashboard")).toHaveCount(0);
  expect((await api(r.page, "GET", "/api/admin/users")).status).toBe(403);
  expect((await api(r.page, "PATCH", "/api/admin/users/" + r.id, { role: "admin" })).status).toBe(403);
  await r.ctx.close();
});

test("the admin promotes a user to agent", async ({ browser }) => {
  const admin = await newUser(browser, { email: adminEmail() });
  await expect(admin.page).toHaveURL(/\/admin/);
  await expect(admin.page.getByTestId("role-badge")).toHaveText("admin");
  const agent = await newUser(browser);
  await admin.page.goto(url("/admin/users"));
  const row = admin.page.locator('[data-testid="user-row"][data-user-id="' + agent.id + '"]');
  await row.getByTestId("user-role-select").selectOption("agent");
  await expect.poll(async () => (await me(agent.page)).role).toBe("agent");
  await agent.page.goto(url("/"));
  await expect(agent.page).toHaveURL(/\/agent/);
  await admin.ctx.close(); await agent.ctx.close();
});

test("workflow: take a ticket, work it, resolve, close", async ({ browser }) => {
  const admin = await newUser(browser, { email: adminEmail() });
  const agent = await newUser(browser);
  await api(admin.page, "PATCH", "/api/admin/users/" + agent.id, { role: "agent" });
  const req = await newUser(browser);
  const t = (await api(req.page, "POST", "/api/tickets", { subject: "Laptop dead", body: "x", priority: "high" })).body;
  expect((await api(req.page, "POST", "/api/tickets/" + t.id + "/transition", { to: "in_progress" })).status).toBe(403);
  await agent.page.goto(url("/agent"));
  await expect(agent.page.getByTestId("queue-unassigned")).toContainText("Laptop dead");
  await agent.page.goto(url("/tickets/" + t.id));
  await agent.page.getByTestId("assign-to-me").click();
  await expect(agent.page.getByTestId("transition-in_progress")).toBeVisible();
  expect((await api(agent.page, "POST", "/api/tickets/" + t.id + "/transition", { to: "closed" })).status).toBe(422);
  await agent.page.getByTestId("transition-in_progress").click();
  await expect(agent.page.getByTestId("ticket-detail-status")).toHaveText("in_progress");
  await agent.page.getByTestId("transition-resolved").click();
  await expect(agent.page.getByTestId("ticket-detail-status")).toHaveText("resolved");
  await req.page.goto(url("/tickets/" + t.id));
  await req.page.getByTestId("transition-closed").click();
  await expect(req.page.getByTestId("ticket-detail-status")).toHaveText("closed");
  expect((await api(req.page, "POST", "/api/tickets/" + t.id + "/transition", { to: "open" })).status).toBe(403);
  await admin.ctx.close(); await agent.ctx.close(); await req.ctx.close();
});

test("internal notes never reach the requester", async ({ browser }) => {
  const admin = await newUser(browser, { email: adminEmail() });
  const req = await newUser(browser);
  const t = (await api(req.page, "POST", "/api/tickets", { subject: "Badge broken", body: "x", priority: "low" })).body;
  const secret = uniq("internal-only-");
  await admin.page.goto(url("/tickets/" + t.id));
  await admin.page.getByTestId("note-input").fill(secret);
  await admin.page.getByTestId("note-submit").click();
  await expect(admin.page.getByTestId("note-item").filter({ hasText: secret })).toHaveCount(1);
  await req.page.goto(url("/tickets/" + t.id));
  await expect(req.page.getByTestId("notes-section")).toHaveCount(0);
  await expect(req.page.locator("body")).not.toContainText(secret);
  expect((await req.page.request.get(url("/api/tickets/" + t.id))).status()).toBeLessThan(300);
  expect(await (await req.page.request.get(url("/api/tickets/" + t.id))).text()).not.toContain(secret);
  expect((await api(req.page, "GET", "/api/tickets/" + t.id + "/notes")).status).toBe(403);
  await admin.ctx.close(); await req.ctx.close();
});
