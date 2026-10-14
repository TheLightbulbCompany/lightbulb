import { describe, expect, it } from "vitest";

import nextConfig, { RETIRED_PAGE_REDIRECTS } from "../../next.config";
import { seoLandingPages } from "@/lib/seo";
import { getAllPosts } from "@/content/blog";

describe("retired public pages", () => {
  it("308s every retired page and no longer publishes it", async () => {
    const redirects = await nextConfig.redirects!();
    for (const r of RETIRED_PAGE_REDIRECTS) {
      expect(redirects).toContainEqual({ ...r, permanent: true });
    }
    const live = new Set([
      ...seoLandingPages.map((p) => `/${p.slug}`),
      ...getAllPosts().map((p) => `/blog/${p.slug}`),
    ]);
    for (const r of RETIRED_PAGE_REDIRECTS) expect(live.has(r.source)).toBe(false);
  });
});

describe("noindex outside the production host", () => {
  it("sends X-Robots-Tag on every host but thelightbulb.company", async () => {
    const headers = await nextConfig.headers!();
    const rule = headers.find((h) => h.headers.some((x) => x.key === "X-Robots-Tag"));
    expect(rule?.missing).toHaveLength(1);
    const host = new RegExp(rule!.missing![0].value!);
    expect(host.test("thelightbulb.company")).toBe(true);
    expect(host.test("dev.thelightbulb.company")).toBe(false);
    expect(host.test("thelightbulbXcompany")).toBe(false);
  });
});
