import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");

describe("ChatLayout — Company nav", () => {
  it("renders CompanyNav gated on the unifiedNav prop via a ternary", () => {
    expect(source).toMatch(/CompanyNav/);
    // Ternary (not bare `&&`) per rendering-conditional-render. Lenient on
    // what follows `?` so prettier's line-wrap (`unifiedNav ? (`) still matches.
    expect(source).toMatch(/unifiedNav\s*\?/);
  });
  it("declares the optional unifiedNav prop", () => {
    expect(source).toMatch(/unifiedNav\?\s*:\s*boolean/);
  });
  it("keeps the Teams pill fallback intact", () => {
    expect(source).toMatch(/className="teams-pill"/);
  });
  it("renders CompanyNav in BOTH the chat and control sidebar branches so it stays visible for inline Teams panels", () => {
    // Selecting a Company item flips the view to "control"; CompanyNav must
    // also render in the control branch, or it would unmount on click and the
    // user would lose the Dashboard/Activity nav + active highlight.
    const renderSites = source.match(/<CompanyNav\b/g) ?? [];
    expect(renderSites.length).toBeGreaterThanOrEqual(2);
  });
});
