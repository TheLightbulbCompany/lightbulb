// Derived from Dyad (https://github.com/dyad-sh/dyad), commit 0ee4a524, benchmarks/app-builder/cuj-tests/portalis/checkpoint-1.spec.ts. Copyright (c) Dyad contributors. Apache-2.0. Modified by The Lightbulb Company.
import { test, expect } from "@playwright/test";
import { signUp, newUser, api, me, url, uniq } from "../_shared/helpers";

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/;

async function createOrg(page: import("@playwright/test").Page, name: string, slug: string) {
  await page.goto(url("/orgs/new"));
  await page.getByTestId("org-name-input").fill(name);
  await page.getByTestId("org-slug-input").fill(slug);
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("create-org-submit").click()]); // let the write land before the next step
}

test("new user sees the empty state, creates an org", async ({ page }) => {
  await signUp(page);
  await expect(page).toHaveURL(/\/orgs$/);
  await expect(page.getByTestId("orgs-empty-state")).toBeVisible();
  await page.getByTestId("create-org-link").click();
  const name = uniq("Acme ");
  await createOrg(page, name, uniq("acme-"));
  await expect(page.getByTestId("org-header-name")).toHaveText(name);
  await expect(page).toHaveURL(UUID);
});

test("duplicate slug is refused", async ({ page, browser }) => {
  await signUp(page);
  const slug = uniq("dup-");
  await createOrg(page, "First", slug);
  await expect(page.getByTestId("org-header-name")).toBeVisible();
  const other = await newUser(browser);
  await createOrg(other.page, "Second", slug);
  await expect(other.page.getByTestId("create-org-error")).toBeVisible();
  expect((await api(other.page, "GET", "/api/orgs")).body).toEqual([]);
  await other.ctx.close();
});

test("settings persist; members table shows the creator as org_admin", async ({ page }) => {
  await signUp(page);
  const name = uniq("Globex ");
  await createOrg(page, name, uniq("globex-"));
  await page.getByTestId("nav-settings").click();
  await page.getByTestId("settings-name-input").fill(name + " Ltd");
  await page.getByTestId("settings-description-input").fill("We make things");
  await page.getByTestId("settings-save").click();
  await expect(page.getByTestId("settings-saved")).toBeVisible();
  await page.goto(url("/orgs"));
  await expect(page.getByTestId("org-card-name").first()).toHaveText(name + " Ltd");
  await page.getByTestId("org-card").first().click();
  await page.getByTestId("nav-members").click();
  await expect(page.getByTestId("member-count")).toContainText("1");
  const row = page.getByTestId("member-row");
  await expect(row).toHaveCount(1);
  await expect(row.getByTestId("member-role")).toHaveText("org_admin");
  await expect(row).toHaveAttribute("data-user-id", (await me(page)).id);
});

test("a non-member sees nothing of another org", async ({ page, browser }) => {
  await signUp(page);
  const name = uniq("Secret ");
  await createOrg(page, name, uniq("secret-"));
  const orgId = (await api(page, "GET", "/api/orgs")).body[0].id;
  const other = await newUser(browser);
  await other.page.goto(url("/orgs/" + orgId));
  await expect(other.page.getByTestId("not-authorized")).toBeVisible();
  await expect(other.page.locator("body")).not.toContainText(name);
  await other.page.goto(url("/orgs/" + orgId + "/members"));
  await expect(other.page.getByTestId("members-table")).toHaveCount(0);
  expect((await api(other.page, "GET", "/api/orgs")).body).toEqual([]);
  await other.ctx.close();
});
