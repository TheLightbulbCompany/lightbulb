// getStartedContent.test.ts
import { describe, it, expect } from "vitest";
import { STEPS } from "../getStartedConfig";
import { ARTICLES } from "../getStartedContent";

describe("get started content", () => {
  it("defines exactly the five core steps in order", () => {
    expect(STEPS.map((s) => s.id)).toEqual(["meet", "chat", "task", "hire", "channel"]);
  });
  it("every action step has a CTA label", () => {
    for (const s of STEPS.filter((s) => s.id !== "meet")) {
      expect(s.ctaLabel.length).toBeGreaterThan(0);
    }
  });
  it("defines six articles with unique ids", () => {
    const ids = ARTICLES.map((a) => a.id);
    expect(ids).toHaveLength(6);
    expect(new Set(ids).size).toBe(6);
  });
});
