import { describe, expect, it } from "vitest";
import { outlineOf } from "../documentOutline";

describe("outlineOf", () => {
  it("lists h1–h3 in order and skips headings inside fences", () => {
    const md = "# Plan\n\ntext\n\n## Goal\n\n```md\n# not a heading\n```\n\n### Week 1\n\n#### too deep\n";
    expect(outlineOf(md)).toEqual([{ level: 1, text: "Plan" }, { level: 2, text: "Goal" }, { level: 3, text: "Week 1" }]);
  });
});
