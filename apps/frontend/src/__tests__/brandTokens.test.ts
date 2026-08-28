import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { scan } from "../../scripts/brand-tokens-scan.mjs";

/**
 * Keeps the brand-tokens sweep (docs/superpowers/specs/2026-08-28-app-brand-tokens.md)
 * done: no colour literal or Tailwind palette class outside the token layer,
 * and no retired font CSS var outside the landing. Run the CLI directly
 * (`node scripts/brand-tokens-scan.mjs src`) for the same check with a
 * `file:line` report.
 */
describe("brand tokens sweep", () => {
  it("has zero colour/font literals outside the token layer", () => {
    const hits = scan(["src"]);
    const report = hits.map((h) => `${h.file}:${h.line}: ${h.match}`).join("\n");
    expect(hits, `\n${report}`).toEqual([]);
  });

  describe("scan() regression: underscore-separated Tailwind arbitrary values", () => {
    let dir: string;

    afterEach(() => {
      if (dir) rmSync(dir, { recursive: true, force: true });
    });

    it("still catches rgba( immediately after an underscore", () => {
      // `\w` includes `_`, so a naive `(?<!\w)` lookbehind lets a Tailwind
      // arbitrary value like `shadow-[0_2px_8px_rgba(6,64,43,.5)]` slip past
      // undetected — the underscore reads as "preceded by a word char".
      dir = mkdtempSync(join(tmpdir(), "brand-tokens-scan-"));
      const file = join(dir, "fixture.tsx");
      writeFileSync(file, 'className="shadow-[0_2px_8px_rgba(6,64,43,.5)]"\n');

      const hits = scan([file]);

      expect(hits).toEqual([{ file, line: 1, match: "rgba(" }]);
    });
  });
});
