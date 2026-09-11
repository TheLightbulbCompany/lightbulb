#!/usr/bin/env node
/**
 * Brand-token scanner. Fails on any colour literal outside the files that are
 * allowed to hold one, plus retired font CSS vars outside the landing.
 *
 * The exemptions (SKIP below) are deliberate, and six are documented in
 * CLAUDE.md and .interface-design/system.md: `app/globals.css` and
 * `app/landing.css`, `app/landing-v2.css` (the token layer itself + marketing),
 * `lib/clerkAppearance.ts` (Clerk takes hex, not var()), `*Brand.ts` (vendor
 * marks, off-palette by definition), `components/landing/**` (marketing, its
 * own palette), and tests (`__tests__/`, `*.test.*`) which hold literals as
 * fixtures.
 *
 * Usage: node scripts/brand-tokens-scan.mjs [paths...]   (default: src)
 * Exit 1 if anything matched. `--count` prints only the total.
 *
 * Also exports `scan(paths, { skip })` -> [{file, line, match}] for the
 * vitest guard (src/__tests__/brandTokens.test.ts).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

const ROOT = process.cwd();
const EXTS = new Set([".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css"]);

export const SKIP = (rel) =>
  rel === "app/globals.css" ||
  rel === "app/landing.css" ||
  rel === "app/landing-v2.css" ||
  rel === "lib/clerkAppearance.ts" ||
  rel.endsWith("Brand.ts") ||
  rel.startsWith("components/landing/") ||
  rel.split("/").includes("__tests__") ||
  /\.test\.[a-z]+$/.test(rel);

const PALETTE =
  "zinc|gray|slate|neutral|stone|emerald|green|red|rose|amber|yellow|orange|sky|blue|indigo|violet|purple|pink|fuchsia|cyan|teal|lime";
const UTIL =
  "bg|text|border|ring|from|to|via|fill|stroke|shadow|outline|decoration|divide|placeholder|caret|accent";
const RETIRED_FONTS = "dm-sans|geist-sans|host-grotesk|epilogue|lora-serif";

const RULES = [
  // 3/4/6/8 digits — an 8-digit #RRGGBBAA is the realistic CSS miss: a plain
  // `{3}){1,2}` cannot reach it, and its own \b lookahead hides it.
  { re: /#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b(?![\w-])/g, skipComments: true },
  { re: /(?<![A-Za-z0-9-])(rgba?|oklch|hsla?)\(/g, skipComments: false }, // not \b/\w: `_rgba(` inside a Tailwind arbitrary value (e.g. shadow-[0_2px_8px_rgba(...)]) must still match — \w includes `_`
  { re: new RegExp(`\\b(${UTIL})-(${PALETTE})-\\d{2,3}\\b`, "g"), skipComments: false },
  // every utility, not just bg/text/border: `fill-black`, `to-black` and
  // `outline-black` are pure black and used to land silently.
  { re: new RegExp(`\\b(${UTIL})-(white|black)\\b`, "g"), skipComments: false },
  { re: /\[#[0-9a-fA-F]{3,8}\]/g, skipComments: false },
  // named CSS colours in attributes / declarations (fill="white", color: red)
  { re: /(?:\b(?:fill|stroke|color|background(?:-color)?|border(?:-color)?)\s*[:=]\s*["']?\s*)(white|black|red|green|blue|gray|grey|orange|yellow|purple|pink|silver|navy|teal|lime|maroon)(?![\w-])/g, skipComments: true },
  // retired font vars — the sanctioned names are --font-sans/--font-serif/--font-mono
  { re: new RegExp(`var\\(--font-(${RETIRED_FONTS})\\)`, "g"), skipComments: false },
];

const isComment = (line) => /^\s*(\/\/|\/\*|\*)/.test(line);

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name === ".next") continue;
      yield* walk(full);
    } else if (EXTS.has(entry.name.slice(entry.name.lastIndexOf(".")))) {
      yield full;
    }
  }
}

export function scan(paths, { skip = SKIP } = {}) {
  const roots = paths && paths.length ? paths : ["src"];
  const hits = [];
  for (const root of roots) {
    const base = statSync(root).isDirectory() ? root : null;
    const files = base ? walk(base) : [root];
    for (const file of files) {
      const posix = relative(ROOT, file).split(sep).join("/");
      const rel = posix.replace(/^(.*\/)?src\//, ""); // skip list is src-relative whatever root was passed
      if (skip(rel)) continue;
      const lines = readFileSync(file, "utf8").split("\n");
      lines.forEach((line, i) => {
        const comment = isComment(line);
        const found = [];
        for (const { re, skipComments } of RULES) {
          if (skipComments && comment) continue;
          re.lastIndex = 0;
          let m;
          while ((m = re.exec(line)) !== null) {
            found.push({ start: m.index, end: m.index + m[0].length, text: m[0] });
          }
        }
        // `text-[#5b5b5b]` trips both the hex rule and the arbitrary-value rule;
        // it is ONE literal to fix, so drop a match nested inside another.
        for (const a of found) {
          if (found.some((b) => b !== a && b.start <= a.start && b.end >= a.end)) continue;
          hits.push({ file, line: i + 1, match: a.text });
        }
      });
    }
  }
  return hits;
}

function main() {
  const args = process.argv.slice(2);
  const countOnly = args.includes("--count");
  const roots = args.filter((a) => !a.startsWith("--"));

  const hits = scan(roots);

  if (countOnly) console.log(hits.length);
  else {
    for (const h of hits) console.log(`${h.file}:${h.line}: ${h.match}`);
    console.log(`\n${hits.length} colour literal(s) outside the token layer.`);
  }
  process.exit(hits.length ? 1 : 0);
}

if (import.meta.url === `file://${process.argv[1]}`) main();
