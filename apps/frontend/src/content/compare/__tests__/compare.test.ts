import { describe, expect, it } from "vitest";

import { COMPARE_PAGES, LIGHTBULB, ROWS } from "@/content/compare";

const ISO = /^\d{4}-\d{2}-\d{2}$/;

describe("comparison pages", () => {
  it("has five pages, each with a unique path", () => {
    expect(COMPARE_PAGES).toHaveLength(5);
    expect(new Set(COMPARE_PAGES.map((p) => p.path)).size).toBe(5);
  });

  it("fails on any cell without a source URL or a checked-on date", () => {
    for (const page of COMPARE_PAGES) {
      for (const col of [LIGHTBULB, ...page.columns]) {
        for (const { key } of ROWS) {
          const cell = col.cells[key];
          const where = `${page.slug} / ${col.name} / ${key}`;
          expect(cell?.text, where).toBeTruthy();
          expect(cell.source, where).toMatch(/^https:\/\//);
          expect(cell.checkedOn, where).toMatch(ISO);
        }
      }
    }
  });

  it("uses none of the banned words", () => {
    // Visible text only: a vendor's URL may contain any word.
    const text = JSON.stringify([COMPARE_PAGES, LIGHTBULB], (k, v) => (k === "source" ? undefined : v));
    const hit = text.match(/multiplayer|AI-native|orchestration|\bprompts?\b|Paperclip|Hermes|AgentCore/i);
    expect(hit?.[0]).toBeUndefined();
  });
});
