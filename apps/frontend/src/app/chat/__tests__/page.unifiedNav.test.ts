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

  // Phase D.2b: under the flag the view is derived purely from ?panel (no
  // tab-switcher), so the activeView derivation short-circuits on unifiedNav.
  it("calls useUnifiedNav() before the activeView derivation uses unifiedNav", () => {
    const hookIdx = source.indexOf("useUnifiedNav()");
    const deriveIdx = source.indexOf("const activeView");
    expect(hookIdx).toBeGreaterThanOrEqual(0);
    expect(deriveIdx).toBeGreaterThanOrEqual(0);
    expect(hookIdx).toBeLessThan(deriveIdx);
  });

  it("derives activeView from ?panel under the flag (unifiedNav ? panelParam ? ...)", () => {
    expect(source).toMatch(
      /const activeView[\s\S]*?unifiedNav\s*\?\s*\(\s*panelParam\s*\?\s*"control"\s*:\s*"chat"\s*\)/,
    );
  });

  it("preserves the flag-off activeView derivation in the else arm", () => {
    expect(source).toMatch(
      /viewParam === "control" \|\| \(viewParam !== "chat" && panelParam\)\s*\?\s*"control"\s*:\s*"chat"/,
    );
  });

  it("defines a clearPanel callback that deletes panel/agent/detailId", () => {
    expect(source).toMatch(/const clearPanel = useCallback/);
    expect(source).toMatch(/clearPanel = useCallback[\s\S]*?params\.delete\("panel"\)/);
    expect(source).toMatch(/clearPanel = useCallback[\s\S]*?params\.delete\("agent"\)/);
    expect(source).toMatch(/clearPanel = useCallback[\s\S]*?params\.delete\("detailId"\)/);
  });

  it("passes onClearPanel={clearPanel} to ChatLayout", () => {
    expect(source).toMatch(/onClearPanel=\{clearPanel\}/);
  });
});
