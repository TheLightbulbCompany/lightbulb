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

// markdown.ts OBSIDIAN_LINK_PATTERN plus an optional #heading we drop.
const WIKILINK = /\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g;

/** A page as the plugin writes it → a page as react-markdown should see it:
 *  `[[target|alias]]` → `[alias](<target>)` (a relative href survives
 *  react-markdown's defaultUrlTransform; `wiki:` would not); the caller
 *  strips frontmatter with `parseFrontmatter` first. */
export function toReadableMarkdown(raw: string): string {
  // ponytail: a plain regex also rewrites a [[link]] inside a code span;
  // move to a remark text-node plugin (see remark-soft-breaks.ts) if a page
  // ever needs to DEMO wikilink syntax in code.
  return raw.replace(
    WIKILINK,
    (_m, target: string, alias?: string) => `[${(alias ?? target).trim()}](<${target.trim()}>)`,
  );
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

// markdown.ts FRONTMATTER_PATTERN, verbatim — plus the capture group, because
// we now READ the block instead of deleting it.
const FRONTMATTER_BLOCK = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

/** The four frontmatter fields the owner is shown. The plugin writes more
 *  (id, title, claims, confidence); everything else is the agent's business. */
export interface VaultMeta {
  pageType?: string;
  status?: string;
  updatedAt?: string;
  sourceIds?: string[];
}

/** A `[[target|alias]]` as data. */
export interface VaultLinkRef {
  target: string;
  alias: string;
}

const META_KEYS = ["pageType", "status", "updatedAt", "sourceIds"] as const;

function unquote(v: string): string {
  return v.trim().replace(/^["']|["']$/g, "").trim();
}

/** Split the plugin's YAML head off a page.
 *
 *  Deliberately NOT a YAML parser: we read three scalars and one flat string
 *  list out of a block another program owns and we never write. A line scan
 *  that ignores everything it doesn't recognise (`claims:` is a list of MAPS)
 *  is smaller than a dependency and cannot fail in a way that loses the page —
 *  the worst case is a pill we don't draw.
 *  // ponytail: line scan, four keys. Reach for a YAML lib only if the panel
 *  // ever needs a nested field (claims[], confidence maps). */
export function parseFrontmatter(markdown: string): { meta: VaultMeta; body: string } {
  const m = FRONTMATTER_BLOCK.exec(markdown);
  if (!m) return { meta: {}, body: markdown };
  const meta: VaultMeta = {};
  let listKey: "sourceIds" | null = null;
  for (const line of m[1].split(/\r?\n/)) {
    const item = /^\s*-\s+(.+)$/.exec(line);
    if (item) {
      if (listKey) (meta.sourceIds ??= []).push(unquote(item[1]));
      continue;
    }
    const kv = /^([A-Za-z][\w-]*):[ \t]*(.*)$/.exec(line);
    if (!kv) continue;
    listKey = null;
    const [, key, raw] = kv;
    if (!(META_KEYS as readonly string[]).includes(key)) continue;
    const value = raw.trim();
    if (key === "sourceIds") {
      if (value.startsWith("[")) {
        meta.sourceIds = value.replace(/^\[|\]$/g, "").split(",").map(unquote).filter(Boolean);
      } else if (!value) {
        meta.sourceIds = [];
        listKey = "sourceIds"; // a block list follows
      }
      continue;
    }
    if (value) meta[key as "pageType" | "status" | "updatedAt"] = unquote(value);
  }
  return { meta, body: markdown.slice(m[0].length) };
}

/** The page's own metadata as up to four neutral chips, in reading order.
 *
 *  Never coloured by value. PanelNotice's header spells out the rule: only
 *  `locked` — the one state the owner is BLOCKED by — takes a state token. A
 *  page's `status` is the PLUGIN's lifecycle field over an open value set we
 *  neither write nor enumerate, and "unknown renders neutral, never ok" makes
 *  neutral the only rule true for all of them. An amber `draft` would make a
 *  healthy first-week brain look broken. */
export function pagePills(meta: VaultMeta): string[] {
  const pills: string[] = [];
  if (meta.pageType) pills.push(meta.pageType);
  if (meta.status) pills.push(meta.status);
  const day = meta.updatedAt ? new Date(meta.updatedAt) : null;
  // Locale AND timezone pinned, for formatVaultDate's reason: the agent writes
  // updatedAt in UTC, and a date that slides a day for a viewer in Tokyo is a
  // fact the owner can't check against the page's own raw text.
  if (day && !Number.isNaN(day.getTime())) {
    pills.push(
      `Updated ${day.toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
        timeZone: "UTC",
      })}`,
    );
  }
  const n = meta.sourceIds?.length ?? 0;
  if (n > 0) pills.push(`${n} ${n === 1 ? "source" : "sources"}`);
  return pills;
}

/** WIKILINK is module-global (`/g`) and shared with toReadableMarkdown's
 *  .replace; matchAll on a fresh clone keeps lastIndex out of the picture. */
function wikilinksIn(text: string): VaultLinkRef[] {
  const out: VaultLinkRef[] = [];
  for (const m of text.matchAll(new RegExp(WIKILINK.source, "g"))) {
    const target = m[1].trim();
    out.push({ target, alias: (m[2] ?? target).trim() });
  }
  return out;
}

// The trailing backlinks section a compiled page carries: a `## Related`
// heading over a wikilink list, wrapped by the plugin's own managed block
// (`<!-- openclaw:wiki:related:start -->`). We slice on the HEADING, not the
// markers, so a hand-written Related list is caught too and the marker names
// stay the plugin's business.
const RELATED_HEADING = /^#{1,6}[ \t]+Related[ \t]*$/im;
const ANY_HEADING = /^#{1,6}[ \t]+\S/m;

/** Lift `## Related` out of the body so the page can render it as a block
 *  instead of as more prose. The section runs to the next heading of any
 *  level, or to the end. An empty one is removed too — a bare "Related"
 *  heading over nothing is noise the compiler, not the owner, left behind. */
export function splitRelated(body: string): { body: string; related: VaultLinkRef[] } {
  const m = RELATED_HEADING.exec(body);
  if (!m) return { body, related: [] };
  const after = body.slice(m.index + m[0].length);
  const next = ANY_HEADING.exec(after);
  const section = next ? after.slice(0, next.index) : after;
  return {
    body: body.slice(0, m.index) + (next ? after.slice(next.index) : ""),
    related: wikilinksIn(section),
  };
}

// The compiled root index: compile rewrites everything between these two
// markers on every wiki_apply, and vault init writes them, so they exist from
// the vault's first moment.
const INDEX_START = "<!-- openclaw:wiki:index:start -->";
const INDEX_END = "<!-- openclaw:wiki:index:end -->";

export interface IndexSection {
  section: string;
  count: number;
  links: VaultLinkRef[];
}

/** The compiled index as data: one entry per `### Sources|Entities|Concepts|
 *  Syntheses|Reports` heading INSIDE the managed block, with the wikilinks
 *  compile listed under it.
 *
 *  `count` is that list's length, not the plugin's own count line: the
 *  headings and the links are what this repo has pinned, the count line's
 *  exact wording is not, and a number derived from the links can never
 *  disagree with the links rendered beside it.
 *
 *  Empty when there is no block, or when the block is the scaffold's
 *  "- No compiled pages yet." — the caller falls back to rendering index.md
 *  as an ordinary page. */
export function parseIndexSections(markdown: string): IndexSection[] {
  const from = markdown.indexOf(INDEX_START);
  const to = markdown.indexOf(INDEX_END);
  if (from === -1 || to === -1 || to < from) return [];
  const block = markdown.slice(from + INDEX_START.length, to);
  const out: IndexSection[] = [];
  for (const part of block.split(/^###[ \t]+/m).slice(1)) {
    const nl = part.indexOf("\n");
    const section = (nl === -1 ? part : part.slice(0, nl)).trim();
    if (!section) continue;
    const links = wikilinksIn(nl === -1 ? "" : part.slice(nl));
    out.push({ section, count: links.length, links });
  }
  return out;
}

/** The n most recently written pages, newest first. Scoped to isFiledPage, so
 *  inbox/ (which wiki_search cannot see) and every index.md (compile's own
 *  output, which would otherwise top this list after EVERY wiki_apply) are
 *  excluded by construction — one predicate, already tested above.
 *  `modified_at` is st_mtime: POSIX SECONDS, which is why formatVaultDate
 *  multiplies by 1000 and so does the renderer. */
export function recentlyUpdated(files: FileEntry[], n = 5): FileEntry[] {
  return files
    .filter(isFiledPage)
    .sort((a, b) => b.modified_at - a.modified_at)
    .slice(0, n);
}
