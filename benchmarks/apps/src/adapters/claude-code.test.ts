import { describe, expect, it } from "vitest";
import { STAGE0_NOTE, STRIPPED, buildRules } from "./claude-code";

const md = ["# Title", "## First move\nhello", ...STRIPPED.map((h) => `## ${h}\nSECRET-${h}`), "## Verify and report\nbye"].join("\n\n");

describe("rules text", () => {
  it("drops the deploy sections and appends the Stage-0 note", () => {
    const r = buildRules(md);
    for (const h of STRIPPED) { expect(r).not.toContain(`## ${h}`); expect(r).not.toContain(`SECRET-${h}`); }
    expect(r).toContain("## First move\nhello");
    expect(r).toContain("## Verify and report\nbye");
    expect(r.endsWith(STAGE0_NOTE)).toBe(true);
    expect(STAGE0_NOTE).toContain("`CRON_SECRET` is set in the environment.");
  });
});
