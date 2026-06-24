import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");

// The unified nav derives the view from ?panel, so the Chat|Control
// tab-switcher is gone — selecting an agent clears ?panel so the content area
// falls back to chat.
describe("ChatLayout — tab-switcher removed", () => {
  it("no longer renders a .tab-switcher block", () => {
    expect(source).not.toMatch(/className="tab-switcher"/);
  });

  it("no longer references the unifiedNav flag", () => {
    expect(source).not.toMatch(/unifiedNav/);
  });

  it("declares the onClearPanel prop on ChatLayoutProps", () => {
    expect(source).toMatch(/onClearPanel\?\s*:\s*\(\)\s*=>\s*void/);
  });

  it("calls onClearPanel from handleSelectAgent (unconditionally)", () => {
    // After dispatching the selectAgent event, clear ?panel so the content area
    // shows chat. Optional-chaining call, no flag gate.
    expect(source).toMatch(
      /function handleSelectAgent[\s\S]*?onClearPanel\?\.\(\)/,
    );
  });
});
