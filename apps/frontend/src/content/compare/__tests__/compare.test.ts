import { describe, expect, it } from "vitest";

import { COMPARE_PAGES } from "@/content/compare";
import { BANNED } from "@/content/__tests__/bannedCopy";

describe("comparison pages", () => {
  it("has three pages with unique paths, titles and descriptions of search length", () => {
    expect(COMPARE_PAGES.map((p) => p.slug)).toEqual(["voicemail", "receptionist", "answering-service"]);
    for (const key of ["path", "title", "description", "h1"] as const) {
      expect(new Set(COMPARE_PAGES.map((p) => p[key])).size).toBe(3);
    }
    for (const p of COMPARE_PAGES) {
      expect(p.title.length, p.slug).toBeLessThanOrEqual(60);
      expect(p.description.length, p.slug).toBeLessThanOrEqual(160);
    }
  });

  it("links a source for every third-party figure", () => {
    for (const p of COMPARE_PAGES) {
      for (const r of p.rows) {
        if (/\d/.test(r.other)) expect(r.source, `${p.slug} / ${r.label}`).toMatch(/^https:\/\//);
      }
    }
  });

  it("names no vendor and none of the banned words in our own claims", () => {
    for (const p of COMPARE_PAGES) {
      const ours = JSON.stringify([p.title, p.description, p.intro, p.rows.map((r) => r.ours), p.faq]);
      expect(ours.match(BANNED)?.[0], p.slug).toBeUndefined();
      expect(JSON.stringify(p).match(/Ruby|Smith\.ai|Goodcall|Slack|Bloome|Grok/)?.[0], p.slug).toBeUndefined();
    }
  });
});
