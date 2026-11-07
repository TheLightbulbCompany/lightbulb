// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/relay-crm/checkpoint-2.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, me, url, uniq } from "../_shared/helpers";

async function addDeal(page: import("@playwright/test").Page, title: string, amount: string) {
  await page.goto(url("/deals/new"));
  await page.getByTestId("deal-form-title").fill(title);
  await page.getByTestId("deal-form-amount").fill(amount);
  await page.getByTestId("deal-form-stage").selectOption("lead");
  await page.getByTestId("deal-form-submit").click();
}

test("each person starts in their own workspace", async ({ page }) => {
  await signUp(page, { name: "Riley" });
  const m = await me(page);
  expect(m.memberships).toHaveLength(1);
  expect(m.memberships[0].workspaceName).toBe("Riley's Workspace");
  expect(m.memberships[0].role).toBe("owner");
  expect(m.activeWorkspaceId).toBe(m.memberships[0].workspaceId);
  await expect(page.getByTestId("workspace-current-name")).toHaveText("Riley's Workspace");
});

test("deals sit in columns with counts and totals; the select moves them", async ({ page }) => {
  await signUp(page);
  await addDeal(page, "Big one", "5000");
  await addDeal(page, "Small one", "250");
  await page.goto(url("/deals"));
  const lead = page.getByTestId("kanban-column-lead");
  await expect(lead.getByTestId("column-count")).toHaveText("2");
  await expect(lead.getByTestId("column-total")).toHaveAttribute("data-total", "5250");
  await lead.getByTestId("deal-card").filter({ hasText: "Big one" }).getByTestId("deal-card-stage-select").selectOption("won");
  const won = page.getByTestId("kanban-column-won");
  await expect(won.getByTestId("deal-card").filter({ hasText: "Big one" })).toHaveCount(1);
  await expect(won.getByTestId("column-total")).toHaveAttribute("data-total", "5000");
  await page.reload();
  await expect(lead.getByTestId("column-count")).toHaveText("1");
});

test("invite a teammate; they accept and the workspace appears in their switcher", async ({ browser }) => {
  const owner = await newUser(browser);
  const guest = await newUser(browser);
  const ws = (await me(owner.page)).activeWorkspaceId;
  expect((await api(guest.page, "GET", "/api/workspaces/" + ws + "/members")).status).toBe(403);
  await owner.page.goto(url("/settings/members"));
  await owner.page.getByTestId("invite-email-input").fill(guest.creds.email);
  await owner.page.getByTestId("invite-submit").click();
  await expect(owner.page.getByTestId("pending-invite-row").filter({ hasText: guest.creds.email })).toHaveCount(1);
  await guest.page.goto(url("/invites"));
  await guest.page.getByTestId("invite-row").first().getByTestId("invite-accept-button").click();
  await expect.poll(async () => (await me(guest.page)).memberships.length).toBe(2);
  await guest.page.goto(url("/deals"));
  await guest.page.getByTestId("workspace-switcher").click();
  await expect(guest.page.getByTestId("workspace-switcher-option")).toHaveCount(2);
  await owner.page.reload();
  await expect(owner.page.locator('[data-testid="member-row"][data-user-id="' + guest.id + '"]')).toHaveCount(1);
  await owner.ctx.close(); await guest.ctx.close();
});

test("another workspace's deals are never visible", async ({ page, browser }) => {
  await signUp(page);
  const d = (await api(page, "POST", "/api/deals", { title: "Secret deal", amount: 100, stage: "lead" })).body;
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/deals")).body).toEqual([]);
  expect((await api(other.page, "GET", "/api/deals/" + d.id)).status).toBeGreaterThanOrEqual(400);
  const mine = (await me(page)).activeWorkspaceId;
  expect((await api(other.page, "GET", "/api/workspaces/" + mine + "/members")).status).toBe(403);
  await other.ctx.close();
});
