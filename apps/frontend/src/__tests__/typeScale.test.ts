import { readFileSync } from "node:fs";
import { join } from "node:path";

import { compile } from "tailwindcss";
import { describe, expect, it } from "vitest";

/**
 * The app's type and spacing scale (stage-2 step 6): body 15/22, meta 13/18,
 * small 11/14, title 18/24, sidebar rows 28, page-list rows 36, headers 48.
 * Same components, colours and typeface; only sizes and spacing.
 */
const SRC = join(__dirname, "..");
const read = (p: string) => readFileSync(join(SRC, p), "utf8");

describe("type scale", () => {
  it("sets the app scale on :root[data-chat-theme]", () => {
    const css = read("app/globals.css");
    const decls: Record<string, string> = {};
    for (const block of css.matchAll(/:root\[data-chat-theme\]\s*\{([^}]*)\}/g)) {
      for (const d of block[1].matchAll(/(--text-[\w-]+)\s*:\s*([^;]+);/g)) decls[d[1]] = d[2].trim();
    }
    expect(decls).toEqual({
      "--text-2xs": "11px",
      "--text-2xs--line-height": "14px",
      "--text-caption": "13px",
      "--text-caption--line-height": "18px",
      "--text-ui": "15px",
      "--text-ui--line-height": "22px",
    });
    // Title keeps its 22px base everywhere: it styles display headings.
    expect(css).toMatch(/--text-title: 1\.375rem;\s*--text-title--line-height: 1\.15;/);
  });

  it("text-* utilities read the variables, so the override reaches them", async () => {
    const css = read("app/globals.css");
    const theme = css.match(/@theme([^{]*)\{([^}]*--text-ui:[^}]*)\}/);
    expect(theme).not.toBeNull();
    const compiled = await compile(`@theme${theme![1]}{${theme![2]}} @tailwind utilities;`);
    const out = compiled.build(["text-2xs", "text-caption", "text-ui", "text-title"]);
    for (const t of ["2xs", "caption", "ui", "title"]) {
      expect(out).toContain(`font-size: var(--text-${t});`);
    }
  });
});
