import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve(__dirname, "../ChatLayout.tsx"), "utf8");

describe("ChatLayout — Company nav", () => {
  it("renders CompanyNav unconditionally (one sidebar body)", () => {
    expect(source).toMatch(/<CompanyNav\b/);
  });

  it("renders CompanyNav exactly once (the single unified sidebar body)", () => {
    const renderSites = source.match(/<CompanyNav\b/g) ?? [];
    expect(renderSites).toHaveLength(1);
  });

  it("no longer renders the standalone /teams pill", () => {
    expect(source).not.toMatch(/className="teams-pill"/);
    expect(source).not.toMatch(/href="\/teams"/);
  });
});
