import { describe, expect, it } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { STAGE0_NOTE, STRIPPED, buildRules, claudeArgs, claudeCodeAdapter } from "./claude-code";

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

const real = join(__dirname, "../../../../apps/infra/hermes/skills-src/lightbulb/references/software-projects.md");
describe("adapter", () => {
  it("strips all six headings from the real rules file (catches heading drift)", () => {
    if (!existsSync(real)) return;
    const src = readFileSync(real, "utf8");
    const r = buildRules(src);
    for (const h of STRIPPED) { expect(src, h).toContain(`## ${h}\n`); expect(r, h).not.toContain(`## ${h}\n`); }
  });
  it("runs with --bare", () => {
    expect(claudeArgs("p", "m")[0]).toBe("--bare");
  });
  it("without ANTHROPIC_API_KEY returns error without spawning", async () => {
    const old = process.env.ANTHROPIC_API_KEY;
    delete process.env.ANTHROPIC_API_KEY;
    try {
      const r = await claudeCodeAdapter.build({ id: "x", prompt: "p" } as any, "/nonexistent");
      expect(r.status).toBe("error");
      expect(r.log).toContain("ANTHROPIC_API_KEY");
    } finally { if (old) process.env.ANTHROPIC_API_KEY = old; }
  });
});
