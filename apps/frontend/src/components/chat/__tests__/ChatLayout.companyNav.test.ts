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
});
