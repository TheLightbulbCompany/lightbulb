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

describe("flickmeon.ai, the retired domain", () => {
  async function targetFor(host: string): Promise<string[]> {
    const redirects = await nextConfig.redirects!();
    return redirects
      .filter((r) => r.has?.some((h) => h.type === "host" && new RegExp(h.value!).test(host)))
      .map((r) => `${r.source} -> ${r.destination} ${r.permanent}`);
  }

  it("308s every path, static files included, to the same path on the product site", async () => {
    expect(await targetFor("flickmeon.ai")).toEqual(["/:path* -> https://thelightbulb.company/:path* true"]);
    expect(await targetFor("www.flickmeon.ai")).toEqual(["/:path* -> https://thelightbulb.company/:path* true"]);
    expect(await targetFor("dev.flickmeon.ai")).toEqual(["/:path* -> https://dev.thelightbulb.company/:path* true"]);
    expect(await targetFor("thelightbulb.company")).toEqual([]);
  });
});
