import { test, expect } from "@playwright/test";
import { signUp, newUser, api, url, uniq } from "../_shared/helpers";

test("sign up, add a task, see it listed", async ({ page }) => {
  await signUp(page);
  await expect(page).toHaveURL(/\/tasks$/);
  await expect(page.getByTestId("tasks-empty")).toBeVisible();
  const title = uniq("Buy milk ");
  await page.getByTestId("task-new-button").click();
  await page.getByTestId("task-form-title").fill(title);
  await page.getByTestId("task-form-priority").selectOption("high");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("task-form-submit").click()]); // let the write land before navigating
  await page.goto(url("/tasks"));
  await expect(page.getByTestId("task-row").filter({ hasText: title })).toHaveCount(1);
});

test("a blank title is refused and nothing is saved", async ({ page }) => {
  await signUp(page);
  await page.goto(url("/tasks/new"));
  await page.getByTestId("task-form-submit").click();
  await expect(page.getByTestId("task-form-error")).toBeVisible();
  const r = await api(page, "GET", "/api/tasks");
  expect(r.body).toEqual([]);
});

test("complete, reopen, filter and delete", async ({ page }) => {
  await signUp(page);
  const title = uniq("Pay rent ");
  await api(page, "POST", "/api/tasks", { title, priority: "medium" });
  await page.goto(url("/tasks"));
  await page.getByTestId("task-row").filter({ hasText: title }).getByTestId("task-row-link").click();
  await expect(page.getByTestId("task-detail-title")).toHaveText(title);
  await expect(page.getByTestId("task-detail-status")).toHaveText("open");
  await page.getByTestId("task-complete-button").click();
  await expect(page.getByTestId("task-detail-status")).toHaveText("done");
  await page.goto(url("/tasks"));
  await page.getByTestId("filter-status").selectOption("open");
  await expect(page.getByTestId("task-row").filter({ hasText: title })).toHaveCount(0);
  await page.getByTestId("filter-status").selectOption("done");
  await page.getByTestId("task-row").filter({ hasText: title }).getByTestId("task-row-link").click();
  await page.getByTestId("task-reopen-button").click();
  await expect(page.getByTestId("task-detail-status")).toHaveText("open");
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("task-delete-button").click()]); // let the write land before navigating
  await page.goto(url("/tasks"));
  await expect(page.getByTestId("task-row").filter({ hasText: title })).toHaveCount(0);
});

test("tasks are private to their owner", async ({ page, browser }) => {
  await signUp(page);
  const created = await api(page, "POST", "/api/tasks", { title: "Private", priority: "low" });
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/tasks")).body).toEqual([]);
  expect([403, 404]).toContain((await api(other.page, "GET", "/api/tasks/" + created.body.id)).status);
  await other.ctx.close();
  const anon = await browser.newContext();
  expect((await anon.request.get(url("/api/tasks"))).status()).toBe(401);
  await anon.close();
});
