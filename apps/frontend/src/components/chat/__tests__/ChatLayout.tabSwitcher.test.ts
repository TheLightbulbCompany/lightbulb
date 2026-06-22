import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");

// Phase D.2b: under the unified nav the Chat|Control tab-switcher is removed —
// the view is derived from ?panel instead. The .tab-switcher block is gated so
// it only renders on the flag-off path, and selecting an agent clears ?panel so
// the content area falls back to chat.
describe("ChatLayout — tab-switcher removal (Phase D.2b)", () => {
  it("gates the .tab-switcher block on !unifiedNav (flag-off only)", () => {
    // Ternary (not bare `&&`) per repo lint convention. Tolerant of `? (` and
    // any whitespace between `!unifiedNav` and the wrapped tab-switcher block.
    expect(source).toMatch(/!unifiedNav\s*\?\s*\([\s\S]*?className="tab-switcher"/);
  });

  it("declares the onClearPanel prop on ChatLayoutProps", () => {
    expect(source).toMatch(/onClearPanel\?\s*:\s*\(\)\s*=>\s*void/);
  });

  it("destructures onClearPanel in the component signature", () => {
    expect(source).toMatch(/onClearPanel/);
  });

  it("calls onClearPanel from handleSelectAgent under unifiedNav", () => {
    // After dispatching the selectAgent event, clear ?panel so the content area
    // shows chat. Optional-chaining call, gated on the flag.
    expect(source).toMatch(
      /function handleSelectAgent[\s\S]*?if\s*\(\s*unifiedNav\s*\)\s*onClearPanel\?\.\(\)/,
    );
  });

  it("keeps the tab-switcher buttons (flag-off path still uses onViewChange)", () => {
    expect(source).toMatch(/onViewChange\("chat"\)/);
    expect(source).toMatch(/onViewChange\("control"\)/);
  });
});
