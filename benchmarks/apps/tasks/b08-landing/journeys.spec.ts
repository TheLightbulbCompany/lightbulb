import { test, expect } from "@playwright/test";
import { url } from "../_shared/helpers";

test("the five sections are there in order", async ({ page }) => {
  await page.goto(url("/"));
  const ids = ["section-hero", "section-features", "section-pricing", "section-faq", "section-contact"];
  const tops: number[] = [];
  for (const id of ids) {
    const el = page.getByTestId(id);
    await expect(el).toBeVisible();
    tops.push((await el.boundingBox())!.y);
  }
  expect([...tops].sort((a, b) => a - b)).toEqual(tops);
  await expect(page.locator("h1")).toHaveCount(1);
});

test("search and sharing basics are in place", async ({ page, request }) => {
  await page.goto(url("/"));
  await expect(page.locator("html")).toHaveAttribute("lang", /\w+/);
  expect((await page.title()).trim().length).toBeGreaterThan(3);
  const desc = (await page.locator('meta[name="description"]').getAttribute("content")) ?? "";
  expect(desc.length).toBeGreaterThanOrEqual(50);
  expect(desc.length).toBeLessThanOrEqual(160);
  await expect(page.locator('meta[property="og:title"]')).toHaveCount(1);
  await expect(page.locator('meta[property="og:description"]')).toHaveCount(1);
  await expect(page.locator('link[rel="canonical"]')).toHaveCount(1);
  for (const img of await page.locator("img").all()) expect(await img.getAttribute("alt")).not.toBeNull();
  expect((await request.get(url("/robots.txt"))).status()).toBe(200);
  const sm = await request.get(url("/sitemap.xml"));
  expect(sm.status()).toBe(200);
  expect(await sm.text()).toContain("<urlset");
});

test("contact form validates then thanks", async ({ page }) => {
  await page.goto(url("/"));
  await page.getByTestId("contact-name").fill("Sam");
  await page.getByTestId("contact-email").fill("not-an-email");
  await page.getByTestId("contact-message").fill("Do you do gluten free?");
  await page.getByTestId("contact-submit").click();
  await expect(page.getByTestId("contact-error")).toBeVisible();
  await expect(page.getByTestId("contact-success")).toHaveCount(0);
  await page.getByTestId("contact-email").fill("sam@example.test");
  await page.getByTestId("contact-submit").click();
  await expect(page.getByTestId("contact-success")).toBeVisible();
});
