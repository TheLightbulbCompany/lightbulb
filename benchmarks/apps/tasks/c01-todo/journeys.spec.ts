import { test, expect } from "@playwright/test";
import { signUp, newUser, api, url, uniq } from "../_shared/helpers";

async function addProject(page: import("@playwright/test").Page, name: string) {
  await page.goto(url("/projects"));
  await page.getByTestId("project-form-name").fill(name);
  await page.getByTestId("project-form-submit").click();
  await expect(page.getByTestId("project-row").filter({ hasText: name })).toHaveCount(1);
}

test("create projects; a blank name is refused", async ({ page }) => {
  await signUp(page);
  await page.goto(url("/projects"));
  await page.getByTestId("project-form-submit").click();
  await expect(page.getByTestId("project-form-error")).toBeVisible();
  await addProject(page, "Home");
  await addProject(page, "Work");
  await expect(page.getByTestId("project-row")).toHaveCount(2);
});

test("put tasks in projects and filter by project", async ({ page }) => {
  await signUp(page);
  await addProject(page, "Home");
  await addProject(page, "Work");
  for (const [title, proj] of [["Mow lawn", "Home"], ["Send report", "Work"]]) {
    await page.goto(url("/tasks/new"));
    await page.getByTestId("task-form-title").fill(title);
    await page.getByTestId("task-form-project").selectOption({ label: proj });
    await page.getByTestId("task-form-submit").click();
  }
  await page.goto(url("/tasks"));
  await expect(page.getByTestId("task-row").filter({ hasText: "Mow lawn" })).toContainText("Home");
  await page.getByTestId("filter-project").selectOption({ label: "Work" });
  await expect(page.getByTestId("task-row")).toHaveCount(1);
  await expect(page.getByTestId("task-row").first()).toContainText("Send report");
});

test("deleting a project keeps its tasks", async ({ page }) => {
  await signUp(page);
  const p = (await api(page, "POST", "/api/projects", { name: "Temp" })).body;
  const t = (await api(page, "POST", "/api/tasks", { title: "Keep me", priority: "low", projectId: p.id })).body;
  expect(t.projectId).toBe(p.id);
  expect((await api(page, "DELETE", "/api/projects/" + p.id)).status).toBeLessThan(300);
  const after = (await api(page, "GET", "/api/tasks")).body;
  expect(after).toHaveLength(1);
  expect(after[0].projectId).toBeNull();
});

test("projects are private", async ({ page, browser }) => {
  await signUp(page);
  const p = (await api(page, "POST", "/api/projects", { name: "Mine" })).body;
  const other = await newUser(browser);
  expect((await api(other.page, "GET", "/api/projects")).body).toEqual([]);
  expect((await api(other.page, "DELETE", "/api/projects/" + p.id)).status).toBeGreaterThanOrEqual(400);
  const t = await api(other.page, "POST", "/api/tasks", { title: "Sneaky", priority: "low", projectId: p.id });
  expect(t.status >= 400 || t.body.projectId == null).toBe(true);
  await other.ctx.close();
});
