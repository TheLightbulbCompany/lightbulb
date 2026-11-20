import { describe, expect, it } from "vitest";

import nextConfig from "../../next.config";

describe("retired public pages", () => {
  it("retired pages are gone, not redirected", async () => {
    const redirects = await nextConfig.redirects!();
    for (const r of redirects) {
      expect(r.has !== undefined || r.source === "/install" || r.source === "/install.sh").toBe(true);
    }
    for (const path of ["/ai-agent-workspace", "/compare/slack", "/docs/get-started"]) {
      expect(redirects.some((r) => r.source === path)).toBe(false);
    }
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
