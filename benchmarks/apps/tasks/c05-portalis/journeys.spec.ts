// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/portalis/checkpoint-2.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, me, url, uniq } from "../_shared/helpers";

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

async function admin(browser: import("@playwright/test").Browser) {
  const u = await newUser(browser);
  await u.page.goto(url("/orgs/new"));
  await u.page.getByTestId("org-name-input").fill(uniq("Org "));
  await u.page.getByTestId("org-slug-input").fill(uniq("org-"));
  await u.page.getByTestId("create-org-submit").click();
  await expect(u.page).toHaveURL(UUID);
  const orgId = u.page.url().match(UUID)![0];
  return { ...u, orgId };
}

test("invite by link: the invited person joins with exactly the invited role", async ({ browser }) => {
  const a = await admin(browser);
  const invitee = await newUser(browser);
  await a.page.goto(url("/orgs/" + a.orgId + "/members"));
  await a.page.getByTestId("invite-email-input").fill(invitee.creds.email);
  await a.page.getByTestId("invite-role-select").selectOption("org_member");
  await a.page.getByTestId("invite-submit").click();
  const row = a.page.getByTestId("invite-row").filter({ hasText: invitee.creds.email });
  await expect(row.getByTestId("invite-status")).toHaveText("pending");
  const link = (await row.getByTestId("invite-link").innerText()).trim();
  expect(link.startsWith(new URL(url("/")).origin + "/invite/")).toBe(true);
  expect(link.length).toBeGreaterThan(new URL(url("/")).origin.length + 28);
  await invitee.page.goto(link);
  await expect(invitee.page.getByTestId("accept-invite-org-name")).toBeVisible();
  await invitee.page.getByTestId("accept-invite-submit").click();
  await expect.poll(async () => (await me(invitee.page)).memberships.map((m: any) => m.role + "@" + m.orgId)).toContain("org_member@" + a.orgId);
  await a.page.reload();
  await expect(a.page.getByTestId("invite-row").filter({ hasText: invitee.creds.email }).getByTestId("invite-status")).toHaveText("accepted");
  await invitee.page.goto(link);
  const again = invitee.page.getByTestId("accept-invite-submit");
  if (await again.count()) await again.click();
  await expect(invitee.page.getByTestId("accept-invite-error")).toBeVisible();
  await a.ctx.close(); await invitee.ctx.close();
});

test("a revoked invite cannot be accepted", async ({ browser }) => {
  const a = await admin(browser);
  const invitee = await newUser(browser);
  const inv = await api(a.page, "POST", "/api/orgs/" + a.orgId + "/invites", { email: invitee.creds.email, role: "org_admin" });
  expect(inv.status).toBeLessThan(300);
  expect((await api(a.page, "DELETE", "/api/orgs/" + a.orgId + "/invites/" + inv.body.id)).status).toBeLessThan(300);
  expect((await api(invitee.page, "POST", "/api/invites/" + inv.body.token + "/accept")).status).toBeGreaterThanOrEqual(400);
  expect((await me(invitee.page)).memberships).toEqual([]);
  expect((await api(invitee.page, "POST", "/api/invites/not-a-real-token/accept")).status).toBeGreaterThanOrEqual(400);
  await a.ctx.close(); await invitee.ctx.close();
});

test("members cannot do admin things; non-members get 404", async ({ browser }) => {
  const a = await admin(browser);
  const member = await newUser(browser);
  const outsider = await newUser(browser);
  const inv = (await api(a.page, "POST", "/api/orgs/" + a.orgId + "/invites", { email: member.creds.email, role: "org_member" })).body;
  await api(member.page, "POST", "/api/invites/" + inv.token + "/accept");
  expect((await api(member.page, "POST", "/api/orgs/" + a.orgId + "/invites", { email: "x@example.test", role: "org_member" })).status).toBe(403);
  expect((await api(member.page, "PATCH", "/api/orgs/" + a.orgId + "/members/" + member.id, { role: "org_admin" })).status).toBe(403);
  expect((await api(outsider.page, "GET", "/api/orgs/" + a.orgId + "/projects")).status).toBe(404);
  expect((await api(outsider.page, "POST", "/api/orgs/" + a.orgId + "/invites", { email: "x@example.test", role: "org_member" })).status).toBe(404);
  const anon = await browser.newContext();
  expect((await anon.request.get(url("/api/orgs/" + a.orgId + "/projects"))).status()).toBe(401);
  await anon.close();
  await a.ctx.close(); await member.ctx.close(); await outsider.ctx.close();
});

test("projects live in one org, and the switcher lists every org", async ({ browser }) => {
  const a = await admin(browser);
  await a.page.goto(url("/orgs/" + a.orgId + "/projects/new"));
  const name = uniq("Apollo ");
  await a.page.getByTestId("project-name-input").fill(name);
  await Promise.all([a.page.waitForResponse((r) => r.request().method() !== "GET"), a.page.getByTestId("project-create-submit").click()]); // let the write land before navigating
  await a.page.goto(url("/orgs/" + a.orgId + "/projects"));
  await expect(a.page.getByTestId("project-row").filter({ hasText: name })).toHaveCount(1);
  const p = (await api(a.page, "GET", "/api/orgs/" + a.orgId + "/projects")).body[0];
  await a.page.goto(url("/orgs/new"));
  await a.page.getByTestId("org-name-input").fill(uniq("Second "));
  await a.page.getByTestId("org-slug-input").fill(uniq("second-"));
  await a.page.getByTestId("create-org-submit").click();
  await expect(a.page.getByTestId("org-switcher")).toBeVisible();
  const otherOrg = (await me(a.page)).memberships.find((m: any) => m.orgId !== a.orgId).orgId;
  expect((await api(a.page, "GET", "/api/orgs/" + otherOrg + "/projects")).body).toEqual([]);
  expect((await api(a.page, "PATCH", "/api/orgs/" + otherOrg + "/projects/" + p.id, { name: "hijack" })).status).toBe(404);
  await a.page.getByTestId("org-switcher").click();
  await expect(a.page.getByTestId("org-switcher-option")).toHaveCount(2);
  await a.ctx.close();
});
