import fs from "node:fs";
import path from "node:path";

/**
 * Markdown-with-frontmatter loader shared by the blog and the docs. Content
 * lives as plain `.md` files (slug = filename); every frontmatter value is
 * JSON (`title: "…"`, `tags: ["a"]`, `order: 1`), a bare string otherwise.
 * Reads happen at build time only — every consumer is statically generated
 * (`dynamicParams = false`), so the files never need to ship with the bundle.
 */
export interface MarkdownDoc<M> {
  slug: string;
  meta: M;
  content: string;
}

const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n)*/;

export function parseFrontmatter(raw: string): { meta: Record<string, unknown>; content: string } {
  const m = FRONTMATTER.exec(raw);
  if (!m) return { meta: {}, content: raw };
  const meta: Record<string, unknown> = {};
  for (const line of m[1].split("\n")) {
    const i = line.indexOf(":");
    if (i < 1) continue;
    const value = line.slice(i + 1).trim();
    try {
      meta[line.slice(0, i).trim()] = JSON.parse(value);
    } catch {
      meta[line.slice(0, i).trim()] = value;
    }
  }
  return { meta, content: raw.slice(m[0].length) };
}

/** `sub` is a folder under `src/content`. The literal prefix keeps Turbopack's
 * file tracing scoped to that tree instead of the whole project. */
export function loadMarkdownDir<M>(sub: string): MarkdownDoc<M>[] {
  const abs = path.join(process.cwd(), "src/content", sub);
  return fs
    .readdirSync(abs)
    .filter((f) => f.endsWith(".md"))
    .map((f) => {
      const { meta, content } = parseFrontmatter(fs.readFileSync(path.join(abs, f), "utf8"));
      return { slug: f.slice(0, -3), meta: meta as M, content };
    });
}

/** Rough read time at ~220 words/minute, never below 1. */
export function readingMinutes(markdown: string): number {
  return Math.max(1, Math.round(markdown.split(/\s+/).length / 220));
}
