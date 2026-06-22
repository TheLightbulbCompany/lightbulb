import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../page.tsx"), "utf8");

describe("chat page — unified-nav branch", () => {
  it("imports the inline Teams panel + the flag hook", () => {
    expect(source).toMatch(/InlineTeamsPanel/);
    expect(source).toMatch(/isTeamsPanelKey/);
    expect(source).toMatch(/useUnifiedNav/);
  });
  it("gates the inline branch on BOTH the flag and a teams- key", () => {
    expect(source).toMatch(/unifiedNav\s*&&\s*isTeamsPanelKey\(activePanel\)/);
  });
  it("still passes the flag down to ChatLayout", () => {
    expect(source).toMatch(/unifiedNav=\{unifiedNav\}/);
  });
});
