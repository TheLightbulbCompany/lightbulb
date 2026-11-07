import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

import { compile } from "tailwindcss";
import { describe, expect, it } from "vitest";

/**
 * The app's type and spacing scale (stage-2 step 6): body 15/22, meta 13/18,
 * small 11/14, title 18/24, sidebar rows 28, page-list rows 36, headers 48.
 * Same components, colours and typeface; only sizes and spacing.
 */
const SRC = join(__dirname, "..");
const read = (p: string) => readFileSync(join(SRC, p), "utf8");

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name !== "__tests__") walk(p, out);
    } else if (/\.(tsx?|css)$/.test(name) && !/\.test\./.test(name)) {
      out.push(p);
    }
  }
  return out;
}

const BANNED = [
  /(?<![\w-])text-\[1[01]px\]/g,
  /(?<![\w-])text-xs(?![\w-])/g,
  /font-size:\s*(?:1[012]px|0?\.625rem|0?\.6875rem|0?\.75rem)/g,
];

describe("type scale", () => {
  it("teams and messaging use no sub-scale text (10/11/12px, text-xs)", () => {
    const hits: string[] = [];
    for (const dir of ["components/teams", "components/messaging"]) {
      for (const file of walk(join(SRC, dir))) {
        read(relative(SRC, file))
          .split("\n")
          .forEach((line, i) => {
            for (const re of BANNED) {
              for (const m of line.matchAll(re)) hits.push(`${relative(SRC, file)}:${i + 1}: ${m[0]}`);
            }
          });
      }
    }
    expect(hits, `\n${hits.join("\n")}`).toEqual([]);
  });

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

  it("headers are 48px and the sidebar row is 28px", () => {
    const messaging = read("components/messaging/messaging.css");
    const workspace = read("components/workspace/workspace.css");
    expect(messaging).toMatch(/\.room-header \{[^}]*min-height: 48px;/);
    expect(messaging).toMatch(/\.room-context-header \{[^}]*min-height: 48px;/);
    expect(messaging).toMatch(/\.room-directory-entry \{[^}]*min-height: 28px;/);
    expect(messaging).toMatch(/\.room-history \{ --room-gutter: 20px;/);
    expect(workspace).toMatch(/\.workspace-header \{[^}]*height: 48px;\s*padding: 0 20px;/);
    expect(messaging).toMatch(/\.room-context-header \{[^}]*padding: 8px 20px;/);
    expect(messaging).toMatch(/\.room-context-body \{[^}]*padding: 12px 20px;/);
    expect(messaging).toMatch(/\.room-details-tabs \{[^}]*padding: 8px 20px;/);
    expect(workspace).toMatch(/\.sidebar-header \{\s*height: 48px;/);
  });

  it("Work page list rows are one 36px line", () => {
    for (const f of ["components/teams/shell/PanelDataTable.tsx"]) {
      expect(read(f), f).toMatch(/(?<![\w-])(?:min-)?h-9(?![\w-])/);
    }
  });
});
