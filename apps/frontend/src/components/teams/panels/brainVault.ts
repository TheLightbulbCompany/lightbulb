// Pure helpers over the memory-wiki vault as routers/brain.py serves it
// (paths relative to wiki/main/). Every constant here mirrors the plugin —
// extensions/memory-wiki/src/{query,markdown,vault}.ts in the agent fork —
// so the Brain never claims more than wiki_search can see.

import type { FileEntry } from "@/hooks/useWorkspaceFiles";

/** The directories wiki_search indexes (query.ts QUERY_DIRS). `inbox/` is deliberately absent. */
export const QUERY_DIRS = ["entities", "concepts", "sources", "syntheses", "reports"] as const;
/** Owner drop-zone (brain.py upload route). Un-indexed until the agent files it. */
export const INBOX_DIR = "inbox";
/** The plugin's root index — created on vault init, rewritten on every compile. */
export const LANDING_PATH = "index.md";

// markdown.ts FRONTMATTER_PATTERN, verbatim.
const FRONTMATTER = /^---\r?\n[\s\S]*?\r?\n---\r?\n?/;
// markdown.ts OBSIDIAN_LINK_PATTERN plus an optional #heading we drop.
const WIKILINK = /\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g;

/** A page as the plugin writes it → a page as react-markdown should see it:
 *  frontmatter gone, `[[target|alias]]` → `[alias](<target>)` (a relative
 *  href survives react-markdown's defaultUrlTransform; `wiki:` would not). */
export function toReadableMarkdown(raw: string): string {
  // ponytail: a plain regex also rewrites a [[link]] inside a code span;
  // move to a remark text-node plugin (see remark-soft-breaks.ts) if a page
  // ever needs to DEMO wikilink syntax in code.
  return raw
    .replace(FRONTMATTER, "")
    .replace(WIKILINK, (_m, target: string, alias?: string) => `[${(alias ?? target).trim()}](<${target.trim()}>)`);
}

function topDir(path: string): string {
  const i = path.indexOf("/");
  return i === -1 ? "" : path.slice(0, i);
}

/** query.ts listWikiMarkdownFiles, exactly: a .md under a QUERY_DIR that is not an index.md. */
export function isFiledPage(e: FileEntry): boolean {
  return (
    e.type === "file" &&
    e.name.endsWith(".md") &&
    e.name !== "index.md" &&
    (QUERY_DIRS as readonly string[]).includes(topDir(e.path))
  );
}

export function isInboxFile(e: FileEntry): boolean {
  return e.type === "file" && topDir(e.path) === INBOX_DIR;
}

/** Plugin plumbing (`.openclaw-wiki/log.jsonl`) the owner never needs to find by name. */
export function isPlumbing(e: FileEntry): boolean {
  return e.path.split("/").some((seg) => seg.startsWith("."));
}

/** filed = what wiki_search indexes; knowledge = filed minus the auto-maintained
 *  reports/ dashboards (compile writes four of them into an otherwise empty
 *  vault, so "empty" must be judged on the other four sections); inbox = the
 *  un-indexed drop-zone. */
export function vaultCounts(files: FileEntry[]): { filed: number; knowledge: number; inbox: number } {
  const filed = files.filter(isFiledPage);
  return {
    filed: filed.length,
    knowledge: filed.filter((e) => topDir(e.path) !== "reports").length,
    inbox: files.filter(isInboxFile).length,
  };
}

function isVaultHref(href: string): boolean {
  return !/^[a-z][a-z0-9+.-]*:|^\/|^#/i.test(href);
}

/** `[[entities/ada-lovelace|Ada]]` → entities/ada-lovelace.md; a bare
 *  `[[ada-lovelace]]` → the first file with that name (Obsidian's short-link
 *  rule; the plugin itself also matches on title/id, which the tree can't
 *  see). Unknown targets come back as a best-guess path with exists:false so
 *  the link still navigates — into the missing-page state, never a 404 page.
 *  Null for anything that is not a vault link (external, in-shell, anchor). */
export function resolveVaultLink(
  href: string,
  files: FileEntry[],
): { path: string; exists: boolean } | null {
  if (!isVaultHref(href)) return null;
  let target = href;
  try { target = decodeURIComponent(href); } catch { /* keep raw */ }
  target = target.split("#")[0].replace(/^\.\//, "").replace(/\/+$/, "").trim();
  if (!target) return null;
  const lower = target.toLowerCase();
  const withMd = lower.endsWith(".md") ? lower : `${lower}.md`;
  const byPath = files.find(
    (f) => f.type === "file" && (f.path.toLowerCase() === lower || f.path.toLowerCase() === withMd),
  );
  if (byPath) return { path: byPath.path, exists: true };
  const stem = withMd.slice(withMd.lastIndexOf("/") + 1);
  const byStem = files.find((f) => f.type === "file" && f.name.toLowerCase() === stem);
  if (byStem) return { path: byStem.path, exists: true };
  return { path: lower.endsWith(".md") ? target : `${target}.md`, exists: false };
}

/** Name search only — it cannot see page contents; the UI says so and hands
 *  content questions to ASK. */
export function searchVault(files: FileEntry[], query: string): FileEntry[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return files
    .filter((f) => f.type === "file" && !isPlumbing(f) && f.path.toLowerCase().includes(q))
    .slice(0, 50);
}

export function askBrainDraft(query: string): string {
  const q = query.trim();
  return q
    ? `Look in our brain for "${q}" and tell me what we know, citing the pages you used.`
    : "Look in our brain and tell me what we know about: ";
}

export function fileInboxDraft(count: number): string {
  const what = count === 1 ? "is 1 file" : `are ${count} files`;
  return `There ${what} in the brain inbox (wiki/main/inbox/) waiting to be filed. Read ${count === 1 ? "it" : "them"} and file what's worth keeping.`;
}

/** `modified_at` is POSIX SECONDS. Pinned to en-US/UTC so the same page reads
 *  the same on every machine — the vault is a shared record, not a local one. */
export function formatVaultDate(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleString("en-US", {
    timeZone: "UTC",
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
