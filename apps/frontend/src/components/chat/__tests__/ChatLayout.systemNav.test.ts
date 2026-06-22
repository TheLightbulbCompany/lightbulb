import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");

describe("ChatLayout — System nav", () => {
  it("imports SystemNav", () => {
    expect(source).toMatch(/import\s*\{\s*SystemNav\s*\}\s*from\s*["'][^"']*SystemNav["']/);
  });

  it("renders SystemNav gated on the unifiedNav prop via a ternary", () => {
    expect(source).toMatch(/SystemNav/);
    // Ternary (not bare `&&`) per rendering-conditional-render.
    expect(source).toMatch(/unifiedNav\s*\?/);
  });

  it("passes activePanel + onPanelChange through to SystemNav", () => {
    expect(source).toMatch(
      /<SystemNav\s+activePanel=\{activePanel\}\s+onPanelChange=\{onPanelChange\}\s*\/>/,
    );
  });

  it("renders SystemNav in BOTH the chat and control sidebar branches", () => {
    // Like CompanyNav, SystemNav must render in both branches so the System
    // section stays visible whichever view is active under the unified nav.
    const renderSites = source.match(/<SystemNav\b/g) ?? [];
    expect(renderSites.length).toBeGreaterThanOrEqual(2);
  });

  it("renders SystemNav immediately after CompanyNav in both branches", () => {
    // Each CompanyNav render site should be immediately followed (within the
    // same gated block) by a SystemNav render site.
    expect(source).toMatch(/<CompanyNav[\s\S]*?<SystemNav[\s\S]*?<CompanyNav[\s\S]*?<SystemNav/);
  });
});
