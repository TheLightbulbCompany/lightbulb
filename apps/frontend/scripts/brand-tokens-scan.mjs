#!/usr/bin/env node
/**
 * Brand-token scanner. Fails on any colour literal outside the files that are
 * allowed to hold one (globals.css, landing.css, clerkAppearance.ts).
 *
 * Usage: node scripts/brand-tokens-scan.mjs [paths...]   (default: src)
 * Exit 1 if anything matched. `--count` prints only the total.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

const ROOT = process.cwd();
const EXTS = new Set([".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css"]);

const SKIP = (rel) =>
  rel === "app/globals.css" ||
  rel === "app/landing.css" ||
  rel === "lib/clerkAppearance.ts" ||
  rel.startsWith("components/landing/") ||
  rel.split("/").includes("__tests__") ||
  /\.test\.[a-z]+$/.test(rel);

const PALETTE =
  "zinc|gray|slate|neutral|stone|emerald|green|red|rose|amber|yellow|orange|sky|blue|indigo|violet|purple|pink|fuchsia|cyan|teal|lime";
const UTIL =
  "bg|text|border|ring|from|to|via|fill|stroke|shadow|outline|decoration|divide|placeholder|caret|accent";

const RULES = [
  { re: /#([0-9a-fA-F]{3}){1,2}\b(?![\w-])/g, skipComments: true },
  { re: /(?<![\w-])(rgba?|oklch|hsla?)\(/g, skipComments: false }, // not \b: `_rgba(` inside a Tailwind arbitrary value must match
  { re: new RegExp(`\\b(${UTIL})-(${PALETTE})-\\d{2,3}\\b`, "g"), skipComments: false },
  { re: /\b(bg|text|border)-(white|black)\b/g, skipComments: false },
  { re: /\[#[0-9a-fA-F]{3,8}\]/g, skipComments: false },
  // named CSS colours in attributes / declarations (fill="white", color: red)
  { re: /(?:\b(?:fill|stroke|color|background(?:-color)?|border(?:-color)?)\s*[:=]\s*["']?\s*)(white|black|red|green|blue|gray|grey|orange|yellow|purple|pink|silver|navy|teal|lime|maroon)(?![\w-])/g, skipComments: true },
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

const args = process.argv.slice(2);
const countOnly = args.includes("--count");
const roots = args.filter((a) => !a.startsWith("--"));
if (roots.length === 0) roots.push("src");

const hits = [];
for (const root of roots) {
  const base = statSync(root).isDirectory() ? root : null;
  const files = base ? walk(base) : [root];
  for (const file of files) {
    const posix = relative(ROOT, file).split(sep).join("/");
    const rel = posix.replace(/^(.*\/)?src\//, ""); // skip list is src-relative whatever root was passed
    if (SKIP(rel)) continue;
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
        hits.push(`${file}:${i + 1}: ${a.text}`);
      }
    });
  }
}

if (countOnly) console.log(hits.length);
else {
  for (const h of hits) console.log(h);
  console.log(`\n${hits.length} colour literal(s) outside the token layer.`);
}
process.exit(hits.length ? 1 : 0);
