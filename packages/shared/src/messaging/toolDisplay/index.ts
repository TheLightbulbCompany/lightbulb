/**
 * Plain-language labels for agent tool calls.
 *
 * The chat transcript used to render the raw internal tool name — users saw
 * `Running bash…` and a pill reading `mcp__openclaw__browser`. This module
 * turns a (name, args) pair into something a person can read:
 *
 *     { name: "read", args: { path: "…/connection_pool.py", offset: 40, limit: 40 } }
 *       → { title: "Read", gerund: "Reading", detail: "lines 40-79 from connection_pool.py" }
 *
 * WHERE THE DATA COMES FROM
 * `tool-display.json` is vendored verbatim from the OpenClaw fork:
 *   apps/shared/OpenClawKit/Sources/OpenClawKit/Resources/tool-display.json
 *   fork tag v2026.7.2-isol8.5 (a4825b1152bf69c4ea76dcaa412252ca549d4cc6, 2026-08-17)
 * Upstream generates it from `src/agents/tool-display-config.ts` and its CI
 * (`pnpm tool-display:check` → `ensureCoreToolCoverage`) HARD-FAILS if any tool
 * in `src/agents/tools/*.ts` lacks display metadata — so the map is complete at
 * the pinned version and stays complete upstream. Ours is a copy, so staleness
 * is on us: it is a frozen snapshot of that tag (the OpenClaw pin is gone).
 *
 * WHY FRONTEND AND NOT BACKEND
 * Folded history blocks are read from `sessions.list` and never pass through
 * `connection_pool._transform_agent_event`. A backend-side mapping would label
 * live tools and leave scrollback raw. Everything needed is already on the
 * wire (`args` rides `tool_start`), so this is a pure client-side derivation.
 * It lives in `@lightbulb/shared` so web and mobile run cards read the same.
 *
 * The detail resolvers below are condensed ports of upstream's
 * `src/agents/tool-display-common.ts`. Upstream's version is 819 lines because
 * it also serves logs, ACP and Discord; this keeps the arms that change what a
 * user reads and drops the rest.
 */
import RAW from "./tool-display.json";

/** The shape of a per-action entry in tool-display.json (e.g. browser.open). */
interface ActionSpec {
  label?: string;
  detailKeys?: string[];
}

/** The shape of a per-tool entry in tool-display.json. */
interface ToolSpec {
  emoji?: string;
  title?: string;
  label?: string;
  detailKeys?: string[];
  actions?: Record<string, ActionSpec>;
}

interface ToolDisplayConfig {
  version: number;
  fallback: { emoji?: string; detailKeys?: string[] };
  tools: Record<string, ToolSpec>;
}

const CONFIG = RAW as ToolDisplayConfig;

/**
 * Coarse bucket used for the settled one-line summary ("Read 6 files · ran 3
 * commands"). Derived from the tool name, not from the args.
 */
export type ToolKind =
  | "read"
  | "write"
  | "edit"
  | "command"
  | "search"
  | "fetch"
  | "browse"
  | "message"
  | "delegate"
  | "generic";

export interface ToolDisplay {
  /** Sentence-case noun form from the config — "Read", "Web Search". */
  title: string;
  /** Present-participle form for a running row — "Reading", "Searching". */
  gerund: string;
  /** Past-tense form for a settled row — "Read", "Searched". */
  past: string;
  /** Short human detail — "connection_pool.py", `for "stripe pricing"`. */
  detail?: string;
  kind: ToolKind;
}

/**
 * Tools reach us under two spellings depending on which harness the container
 * runs: `browser` on the native runner, `mcp__openclaw__browser` on CLI
 * backends, where OpenClaw bridges its own tools over a loopback MCP server
 * (`OPENCLAW_MCP_TOOL_PREFIX`). Upstream's `normalizeToolName` only trims, so
 * this strip is ours — without it every `claude-cli` container falls through to
 * the generic fallback and shows raw names, which is the exact bug being fixed.
 */
const MCP_PREFIX = /^mcp__[a-z0-9_-]+__/i;

export function normalizeToolName(name?: string): string {
  return (name ?? "tool").trim().replace(MCP_PREFIX, "");
}

/**
 * snake_case → Title Case, preserving short all-caps segments (so `read_pdf`
 * becomes "Read PDF", not "Read Pdf"). Ported from upstream `defaultTitle`.
 */
export function defaultTitle(name: string): string {
  const cleaned = name.replace(/_/g, " ").trim();
  if (!cleaned) return "Tool";
  return cleaned
    .split(/\s+/)
    .map((part) =>
      part.length <= 2 && part.toUpperCase() === part
        ? part
        : `${part.charAt(0).toUpperCase()}${part.slice(1)}`,
    )
    .join(" ");
}

/**
 * Verb forms per tool key. The config gives us a noun title ("Read", "Web
 * Search"); a readable row needs "Reading…" live and "Read" once settled, and
 * English is too irregular to derive either from the noun. Only tools a user
 * actually sees often are listed — anything else falls back to the title
 * unchanged, which reads acceptably ("Canvas", "Canvas") if not beautifully.
 */
const VERBS: Record<string, {
  gerund: string;
  past: string;
  kind: ToolKind;
  /** The one arg worth showing, when `detailKeys` would pick the wrong one. */
  detail?: (args: Record<string, unknown>) => string | undefined;
}> = {
  read: { gerund: "Reading", past: "Read", kind: "read" },
  write: { gerund: "Writing", past: "Wrote", kind: "write" },
  edit: { gerund: "Editing", past: "Edited", kind: "edit" },
  apply_patch: { gerund: "Patching", past: "Patched", kind: "edit" },
  bash: { gerund: "Running", past: "Ran", kind: "command" },
  exec: { gerund: "Running", past: "Ran", kind: "command" },
  terminal: { gerund: "Running", past: "Ran", kind: "command" },
  process: { gerund: "Running", past: "Ran", kind: "command" },
  web_search: { gerund: "Searching the web", past: "Searched the web", kind: "search" },
  web_fetch: { gerund: "Fetching", past: "Fetched", kind: "fetch" },
  browser: { gerund: "Browsing", past: "Browsed", kind: "browse" },
  memory_search: { gerund: "Searching memory", past: "Searched memory", kind: "search" },
  memory_get: { gerund: "Recalling", past: "Recalled", kind: "read" },
  sessions_search: { gerund: "Searching sessions", past: "Searched sessions", kind: "search" },
  sessions_history: { gerund: "Reading history", past: "Read history", kind: "read" },
  message: { gerund: "Messaging", past: "Messaged", kind: "message" },
  conversations_send: { gerund: "Sending", past: "Sent", kind: "message" },
  spawn_task: { gerund: "Delegating", past: "Delegated", kind: "delegate" },
  sessions_spawn: { gerund: "Delegating", past: "Delegated", kind: "delegate" },
  subagents: { gerund: "Delegating", past: "Delegated", kind: "delegate" },
  image_generate: { gerund: "Generating an image", past: "Generated an image", kind: "generic" },
  video_generate: { gerund: "Generating video", past: "Generated video", kind: "generic" },
  ask_user: { gerund: "Asking you", past: "Asked you", kind: "message" },
  update_plan: { gerund: "Updating the plan", past: "Updated the plan", kind: "generic" },
  create_goal: { gerund: "Creating a goal", past: "Created a goal", kind: "generic" },
  update_goal: { gerund: "Updating a goal", past: "Updated a goal", kind: "generic" },
  // Hermes names (the runtime since 2026-09): no entry in the vendored OpenClaw config.
  skill_view: { gerund: "Reading skill:", past: "Read skill:", kind: "generic", detail: (a) => firstString(a.name) },
  skills_list: { gerund: "Listing skills", past: "Listed skills", kind: "generic" },
  tool_describe: { gerund: "Looking up a tool", past: "Looked up a tool", kind: "generic", detail: () => undefined },
  execute_code: { gerund: "Running code", past: "Ran code", kind: "command", detail: () => undefined },
  read_file: { gerund: "Reading", past: "Read", kind: "read", detail: readDetail },
  write_file: { gerund: "Writing", past: "Wrote", kind: "write", detail: (a) => writeDetail("write", a) },
  patch: { gerund: "Editing", past: "Edited", kind: "edit", detail: (a) => writeDetail("write", a) },
  search_files: { gerund: "Searching files", past: "Searched files", kind: "search", detail: (a) => firstString(a.pattern, a.query) },
  web_extract: { gerund: "Reading", past: "Read", kind: "fetch", detail: (a) => fetchDetail({ url: Array.isArray(a.urls) ? a.urls[0] : a.url }) },
  browser_navigate: { gerund: "Opening", past: "Opened", kind: "browse", detail: fetchDetail },
  browser_snapshot: { gerund: "Reading the page", past: "Read the page", kind: "browse", detail: () => undefined },
  browser_click: { gerund: "Clicking", past: "Clicked", kind: "browse", detail: () => undefined },
  browser_type: { gerund: "Typing into a field", past: "Typed into a field", kind: "browse", detail: () => undefined },
  browser_vision: { gerund: "Looking at the page", past: "Looked at the page", kind: "browse", detail: () => undefined },
  delegate_task: { gerund: "Delegating", past: "Delegated", kind: "delegate", detail: () => undefined },
  send_room_message: { gerund: "Posting in the room", past: "Posted in the room", kind: "message", detail: () => undefined },
  lightbulb_local_sessions: {
    gerund: "Shared terminal",
    past: "Shared terminal",
    kind: "generic",
    detail: (a) => ({ resources: "· list", read: "· read output", send: "· send input", receipt: "· check a command" })[String(a.action)],
  },
  lightbulb_desktop: { gerund: "Mac", past: "Mac", kind: "generic", detail: desktopDetail },
};

function clip(value: string, max = 60): string {
  const oneLine = value.split(/\r?\n/)[0]!.trim();
  return oneLine.length > max ? `${oneLine.slice(0, max - 1)}…` : oneLine;
}

/**
 * `lightbulb_desktop` multiplexes a paired Mac's shell, browser, files and MCP
 * behind `action` + a nested `args`. Built from the call's own typed args, and
 * never from anything a person or agent typed into a page: `type`/`fill` text
 * can be a password, so those rows name the action only.
 */
function desktopDetail(a: Record<string, unknown>): string | undefined {
  const inner = asRecord(a.args) ?? {};
  switch (a.action) {
    case "devices": return "· list paired Macs";
    case "status": return "· check progress";
    case "cancel": return "· cancel";
    case "mcp_list": return "MCP · list tools";
    case "mcp_call": return `MCP · ${clip([firstString(a.server), firstString(a.tool)].filter(Boolean).join(" "))}`;
    case "file": {
      const op = ({ "file.fetch": "read", "file.write": "write", "dir.list": "list", "dir.fetch": "copy folder" } as Record<string, string>)[String(a.fileAction)] ?? "use";
      const path = firstString(inner.path);
      return `files · ${op}${path ? ` ${shortenPath(path)}` : ""}`;
    }
    case "invoke": {
      if (a.command === "system.run") {
        const argv = Array.isArray(inner.argv) ? inner.argv.filter((v): v is string => typeof v === "string") : [];
        return `shell · ${commandDetail({ command: argv.join(" ") }) ?? "run a command"}`;
      }
      if (a.command === "screen.snapshot") return "· screenshot";
      if (a.command === "system.notify") return "· send a notification";
      return "· check the device";
    }
    case "browser": {
      const op = String(a.browserAction ?? "");
      const target =
        op === "open" || op === "navigate" ? fetchDetail(inner)
        : op === "press" ? firstString(inner.key)
        : op === "wait" ? (firstString(inner.text) ? `for “${clip(String(inner.text), 40)}”` : undefined)
        : undefined;
      const verb = ({ snapshot: "read page", type: "type into a field", fill: "fill a form", select: "choose an option", screenshot: "screenshot" } as Record<string, string>)[op] ?? op;
      return `browser · ${verb}${target ? ` ${clip(target)}` : ""}`;
    }
    default: return undefined;
  }
}

/**
 * Typed text on the wire can be a password (a browser `type`/`fill`, or a
 * shell `env`). Masked before any surface keeps it — the expanded "input" view
 * included. The bridge scrubs the same keys at the source; this covers frames
 * from images that predate that.
 */
export function redactToolArgs(name: string, args: Record<string, unknown>): Record<string, unknown> {
  const key = normalizeToolName(name).toLowerCase();
  if (key === "browser_type" && "text" in args) return { ...args, text: "•••" };
  if (key !== "lightbulb_desktop") return args;
  const inner = asRecord(args.args);
  if (!inner) return args;
  const masked: Record<string, unknown> = { ...inner };
  if ("text" in masked && args.browserAction === "type") masked.text = "•••";
  if (Array.isArray(masked.fields)) masked.fields = masked.fields.map((f) => (asRecord(f) ? { ...f, value: "•••" } : f));
  if (asRecord(masked.env)) masked.env = Object.fromEntries(Object.keys(masked.env as object).map((k) => [k, "•••"]));
  return { ...args, args: masked };
}

/**
 * Consecutive items with the same key fold into one, keeping the LAST (its
 * status and output are the current ones) and a count for the "×N" badge.
 */
export function collapseRepeats<T>(items: readonly T[], key: (item: T) => string): Array<{ item: T; count: number }> {
  const out: Array<{ item: T; count: number; key: string }> = [];
  for (const item of items) {
    const k = key(item);
    const last = out[out.length - 1];
    if (last && last.key === k) {
      last.item = item;
      last.count += 1;
    } else out.push({ item, count: 1, key: k });
  }
  return out.map(({ item, count }) => ({ item, count }));
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function firstString(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (trimmed) return trimmed;
  }
  return undefined;
}

// Named for what it actually accepts, not "positiveInt" — 0 is a real,
// meaningful value here (an offset of 0 is "from the top of the file"), and
// treating it as absent silently misrenders read/search details ("first 40
// lines" instead of "lines 0-39").
function nonNegativeInt(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : undefined;
}

/**
 * Container paths are long and identical across every row
 * (`/mnt/efs/users/user_2abc.../workspaces/agent_.../src/app.ts`). The leading
 * segments carry no information a user wants, so show the basename and enough
 * parent to disambiguate. Absolute paths outside the workspace keep a leading
 * ellipsis so it stays obvious the path was trimmed.
 */
export function shortenPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= 2) return path.replace(/^\.\//, "");
  return `…/${parts.slice(-2).join("/")}`;
}

function readDetail(args: Record<string, unknown>): string | undefined {
  const path = firstString(args.path, args.file_path, args.filePath);
  if (!path) return undefined;
  const short = shortenPath(path);
  const offset = nonNegativeInt(args.offset);
  const limit = nonNegativeInt(args.limit);
  if (offset !== undefined && limit !== undefined) {
    return `lines ${offset}-${offset + limit - 1} from ${short}`;
  }
  if (offset !== undefined) return `${short} from line ${offset}`;
  if (limit !== undefined) return `first ${limit} lines of ${short}`;
  return short;
}

function writeDetail(key: string, args: Record<string, unknown>): string | undefined {
  const path = firstString(args.path, args.file_path, args.filePath, args.url);
  if (!path) return undefined;
  const short = shortenPath(path);
  const content = firstString(args.content, args.newText, args.new_string);
  return content ? `${short} (${content.length} chars)` : short;
}

/**
 * Six spellings because the query arg name varies by search provider; upstream
 * normalizes the same set in `collectWebSearchQueries`.
 */
/**
 * Agents routinely quote an exact phrase inside a query
 * (`"northwind ceramics" existing brands`). Wrapping that in our own curly
 * quotes doubles them up and reads like a typo — observed live on the landing
 * demo as: for “"northwind ceramics" exist…
 *
 * Inner doubles become singles rather than being stripped: the quoting carries
 * meaning (it is exact-phrase search syntax), so dropping it would misreport
 * what the agent actually searched for.
 */
function normalizeInnerQuotes(query: string): string {
  return query.replace(/["\u201c\u201d]/g, "'");
}

function searchDetail(args: Record<string, unknown>): string | undefined {
  const raw = firstString(args.query, args.q, args.search, args.searchQuery, args.text, args.prompt);
  const query = raw === undefined ? undefined : normalizeInnerQuotes(raw);
  if (!query) return undefined;
  const count =
    nonNegativeInt(args.count) ??
    nonNegativeInt(args.max_results) ??
    nonNegativeInt(args.num_results) ??
    nonNegativeInt(args.limit) ??
    nonNegativeInt(args.top_k);
  return count !== undefined ? `for “${query}” (top ${count})` : `for “${query}”`;
}

function fetchDetail(args: Record<string, unknown>): string | undefined {
  const url = firstString(args.url, args.targetUrl, args.href);
  if (!url) return undefined;
  try {
    const parsed = new URL(url);
    return parsed.hostname.replace(/^www\./, "") + (parsed.pathname === "/" ? "" : parsed.pathname);
  } catch {
    return url;
  }
}

/**
 * Shell commands are shown as typed, not paraphrased. Upstream has a
 * command→English phrasebook (`tool-display-exec.ts`, 623 lines) but a
 * developer reading `npm run build` learns more from the literal command than
 * from "build the project", and the phrasebook silently mislabels anything it
 * does not know. Pipelines collapse so a 300-char one-liner cannot blow the row.
 */
function commandDetail(args: Record<string, unknown>): string | undefined {
  const command = firstString(args.command, args.cmd, args.script);
  if (!command) return undefined;
  const oneLine = command.split(/\r?\n/)[0]!.trim();
  const stages = oneLine.split("|");
  const collapsed =
    stages.length > 2
      ? `${stages[0]!.trim()} → ${stages[stages.length - 1]!.trim()} (+${stages.length - 2} more)`
      : oneLine;
  return collapsed.length > 72 ? `${collapsed.slice(0, 71)}…` : collapsed;
}

/**
 * Last resort: the first `detailKeys` entry that holds a usable value. This is
 * what makes the ~40 tools with no bespoke resolver still read sensibly —
 * `cron` shows its schedule, `gateway` its target — because upstream already
 * chose which arg matters per tool.
 */
function detailFromKeys(
  args: Record<string, unknown>,
  keys: string[] | undefined,
): string | undefined {
  if (!keys?.length) return undefined;
  for (const key of keys) {
    const value = args[key];
    if (typeof value === "string") {
      const trimmed = value.trim().split(/\r?\n/)[0]!.trim();
      if (trimmed) return trimmed.length > 72 ? `${trimmed.slice(0, 71)}…` : trimmed;
    }
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return undefined;
}

/**
 * Resolves how one tool call should read in the transcript.
 *
 * Never throws and never returns an empty title: an unrecognized tool falls
 * back to a Title Cased version of its own name, which is still strictly better
 * than the raw identifier we used to print.
 */
export function resolveToolDisplay(name: string | undefined, args: unknown): ToolDisplay {
  const toolName = normalizeToolName(name);
  const key = toolName.toLowerCase();
  const spec: ToolSpec | undefined = CONFIG.tools[key];
  const record = asRecord(args) ?? {};

  // Multiplexed tools (browser, message, cron, …) carry the real operation in
  // `args.action`, and the config holds a label per action. Without this,
  // every one of `browser`'s 20+ actions renders identically as "Browser".
  const action = firstString(record.action);
  const actionSpec = action ? spec?.actions?.[action] : undefined;

  const verbs = VERBS[key];
  const title = spec?.title ?? defaultTitle(toolName);
  const kind = verbs?.kind ?? "generic";

  let gerund = verbs?.gerund ?? title;
  let past = verbs?.past ?? title;
  // An action label is more specific than the tool's own verb ("Browsing" →
  // "Opening"), so it wins when present.
  if (actionSpec?.label) {
    const label = actionSpec.label.replace(/_/g, " ");
    gerund = `${title} — ${label}`;
    past = `${title} — ${label}`;
  }

  let detail: string | undefined;
  if (key === "bash" || key === "exec" || key === "terminal") detail = commandDetail(record);
  if (!detail && key === "read") detail = readDetail(record);
  if (!detail && (key === "write" || key === "edit" || key === "apply_patch" || key === "attach")) {
    detail = writeDetail(key, record);
  }
  if (!detail && (key === "web_search" || key === "memory_search" || key === "sessions_search")) {
    detail = searchDetail(record);
  }
  if (!detail && key === "web_fetch") detail = fetchDetail(record);
  if (verbs?.detail) detail = verbs.detail(record);
  else if (!detail) {
    detail = detailFromKeys(record, actionSpec?.detailKeys ?? spec?.detailKeys ?? CONFIG.fallback.detailKeys);
  }

  return { title, gerund, past, detail, kind };
}

/** One row's phrasing, tense chosen by whether the call has finished. */
export function toolRowLabel(display: ToolDisplay, running: boolean): string {
  const verb = running ? display.gerund : display.past;
  return display.detail ? `${verb} ${display.detail}` : verb;
}

// Verbs are stored LOWERCASE without exception — the join below capitalizes
// only the first clause, so a capitalized verb here would read "Ran 3 commands
// · Read 2 files" with a stray capital mid-sentence. ("Read" is the trap: it is
// spelled the same in both tenses, so a capital looks right in isolation.)
const KIND_NOUNS: Record<ToolKind, { one: string; many: string; verb: string }> = {
  read: { one: "file", many: "files", verb: "read" },
  write: { one: "file", many: "files", verb: "wrote" },
  edit: { one: "file", many: "files", verb: "edited" },
  command: { one: "command", many: "commands", verb: "ran" },
  search: { one: "search", many: "searches", verb: "ran" },
  fetch: { one: "page", many: "pages", verb: "fetched" },
  browse: { one: "page", many: "pages", verb: "browsed" },
  message: { one: "message", many: "messages", verb: "sent" },
  delegate: { one: "task", many: "tasks", verb: "delegated" },
  generic: { one: "step", many: "steps", verb: "ran" },
};

/**
 * The settled one-liner: "Read 6 files · ran 3 commands · edited 2 files".
 *
 * Counts DISTINCT details per kind, not calls — nine edits to three files reads
 * "edited 3 files", which is what a person means when they ask what changed.
 */
export function summarizeToolRun(
  calls: Array<{ display: ToolDisplay; failed?: boolean }>,
): string {
  const byKind = new Map<ToolKind, Set<string>>();
  let failures = 0;
  for (const call of calls) {
    if (call.failed) failures += 1;
    const set = byKind.get(call.display.kind) ?? new Set<string>();
    // Callers with no detail are distinct by position, so they still count.
    set.add(call.display.detail ?? `#${set.size}`);
    byKind.set(call.display.kind, set);
  }

  const parts: string[] = [];
  for (const [kind, set] of byKind) {
    const noun = KIND_NOUNS[kind];
    const n = set.size;
    parts.push(
      parts.length === 0
        ? `${noun.verb.charAt(0).toUpperCase()}${noun.verb.slice(1)} ${n} ${n === 1 ? noun.one : noun.many}`
        : `${noun.verb} ${n} ${n === 1 ? noun.one : noun.many}`,
    );
  }
  if (failures > 0) parts.push(`${failures} failed`);
  return parts.join(" · ");
}
