import { test, expect, type Page } from "@playwright/test";
import { signUp, newUser, api, url, uniq } from "../_shared/helpers";

async function writePost(page: Page, title: string, slug: string, published: boolean) {
  await page.goto(url("/admin/posts/new"));
  await page.getByTestId("post-form-title").fill(title);
  await page.getByTestId("post-form-slug").fill(slug);
  await page.getByTestId("post-form-body").fill("Body of " + title);
  if (published) await page.getByTestId("post-form-published").check();
  await Promise.all([page.waitForResponse((r) => r.request().method() !== "GET"), page.getByTestId("post-form-submit").click()]); // let the write land before navigating on
}

test("editor publishes a post and the public can read it, drafts stay hidden", async ({ page, browser }) => {
  const tag = uniq("p");
  await signUp(page, { email: "editor+" + tag + "@example.test" });
  await expect(page.getByTestId("role-badge")).toHaveText("editor");
  const live = "Live " + tag, draft = "Draft " + tag;
  await writePost(page, live, "live-" + tag, true);
  await writePost(page, draft, "draft-" + tag, false);
  await page.goto(url("/admin/posts"));
  await expect(page.getByTestId("admin-post-row").filter({ hasText: tag })).toHaveCount(2); // the admin list is the whole blog's, so other tests' posts are there too
  const anon = await browser.newContext();
  const pub = await anon.newPage();
  await pub.goto(url("/blog"));
  await expect(pub.getByTestId("blog-post-card").filter({ hasText: live })).toHaveCount(1);
  await expect(pub.getByTestId("blog-post-card").filter({ hasText: draft })).toHaveCount(0);
  await pub.getByTestId("blog-post-card").filter({ hasText: live }).getByTestId("blog-post-link").click();
  await expect(pub.getByTestId("post-title")).toHaveText(live);
  await expect(pub.getByTestId("post-body")).toContainText("Body of " + live);
  const hidden = await pub.goto(url("/blog/draft-" + tag));
  expect(hidden!.status()).toBe(404);
  await anon.close();
});

test("a duplicate slug is refused", async ({ page }) => {
  const tag = uniq("d");
  await signUp(page, { email: "editor+" + tag + "@example.test" });
  await writePost(page, "One " + tag, "same-" + tag, true);
  await writePost(page, "Two " + tag, "same-" + tag, true);
  await expect(page.getByTestId("post-form-error")).toBeVisible();
  const r = await api(page, "POST", "/api/posts", { title: "Three", slug: "same-" + tag, body: "x", published: true });
  expect(r.status).toBeGreaterThanOrEqual(400);
});

test("readers and visitors cannot edit", async ({ page, browser }) => {
  const reader = await newUser(browser);
  await expect(reader.page.getByTestId("role-badge")).toHaveText("reader");
  await reader.page.goto(url("/admin/posts"));
  await expect(reader.page.getByTestId("forbidden-message")).toBeVisible();
  await expect(reader.page.getByTestId("admin-post-row")).toHaveCount(0);
  expect((await api(reader.page, "POST", "/api/posts", { title: "x", slug: uniq("x"), body: "x", published: true })).status).toBe(403);
  await reader.ctx.close();
  const anon = await browser.newContext();
  expect([401, 403]).toContain((await anon.request.post(url("/api/posts"), { data: { title: "x", slug: uniq("y"), body: "x", published: true } })).status());
  await anon.close();
  await page.goto(url("/admin/posts"));
  await expect(page).toHaveURL(/\/auth\/sign-in/);
});
