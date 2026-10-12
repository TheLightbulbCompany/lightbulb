import type { LocalSessionEvent } from "./types";

/**
 * The transcript model shared by the web pane and the iOS screen: turns the
 * canonical event stream of a shared Claude Code / Codex session into the
 * rows each TUI draws. Pure — no React, no DOM.
 */
export type LocalSessionProvider = "claude" | "codex";
export type TranscriptRow =
  | { kind: "prompt"; seq: number; text: string; who: string | null; ts?: string }
  | { kind: "prose"; seq: number; text: string; final: boolean }
  /** `ts` is when the thought ended; `ms` is how long it ran, the gap since the event before it. */
  | { kind: "thinking"; seq: number; text: string; ts?: string; ms?: number }
  | { kind: "tool"; seq: number; call: LocalSessionEvent; result?: LocalSessionEvent }
  | { kind: "notice"; seq: number; text: string }
  | { kind: "lifecycle"; seq: number; lifecycle: NonNullable<LocalSessionEvent["lifecycle"]>; text: string }
  /** A background subagent's end, as its parent saw it: one line that points at its row. */
  | { kind: "subagent"; seq: number; id: string; text: string; failed: boolean; ms?: number }
  /** A subagent, inline where it was launched; it expands into `buildRows(events, subagent.id)`. `earlier`: launched before the loaded window. */
  | { kind: "agent"; seq: number; subagent: SessionSubagent; earlier?: boolean };
export type ToolTranscriptRow = Extract<TranscriptRow, { kind: "tool" }>;
export type ThinkingTranscriptRow = Extract<TranscriptRow, { kind: "thinking" }>;
/**
 * A folded run of tool calls, drawn as one summary line. `lines` are the rows
 * Codex prints under its own summary ("Explored"): always visible, one per call.
 */
export type ToolGroupItem = { kind: "tools"; seq: number; rows: (ToolTranscriptRow | ThinkingTranscriptRow)[]; summary: string; lines?: string[] };
export type TranscriptItem = TranscriptRow | ToolGroupItem;

/** Tool output and diffs show this many lines until the reader expands them. */
export const PREVIEW_LINES = 3;

/** Each TUI's own glyphs; renderers draw exactly these. */
export const GLYPH = {
  claude: { prompt: "❯", bullet: "⏺", output: "⎿", thinking: "✻", working: "✻" },
  codex: { prompt: "›", bullet: "•", output: "└", thinking: "•", working: "•" },
} as const;

// Strips terminal escape sequences (color codes, cursor moves) that ride
// along in raw tool output — they render as garbage in a <pre>.
// The \x1b escape is written textually on purpose so the raw control byte never lives in source.
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
export function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, "");
}

/** A subagent's start or end: what its inline row knows, never a transcript line of its own. */
const isSubagentMarker = (e: LocalSessionEvent) => e.lifecycle === "subagent_start" || e.lifecycle === "subagent_end";
/** The end the parent itself was told about (Claude's task notification), so it draws a line there. */
const isBackgroundEnd = (e: LocalSessionEvent) => e.lifecycle === "subagent_end" && rec(e.tool?.result).background === true;

/** The session's own events (its plan, its pending prompt); everything a subagent did is tagged with it. */
export function mainEvents(events: readonly LocalSessionEvent[]): LocalSessionEvent[] {
  return events.filter(e => !e.subagent);
}

export interface SessionSubagent {
  /** "" for an `Agent` call no mirrored subagent answers (older history): drawn, not expandable. */
  id: string;
  description: string;
  /** Claude's subagent_type, Codex's agent role; null when unknown. */
  type: string | null;
  state: "running" | "completed" | "failed";
  /** Launched to run in the background; null where the harness has no such split (Codex). */
  background: boolean | null;
  startedAt?: string;
  endedAt?: string;
  durationMs?: number;
  /** Tool calls it made, in the loaded window. */
  toolUses: number;
  /** Its most recent tool call, for the live status line. */
  lastStep?: LocalSessionEvent;
  /** The parent's `Agent` call (Claude); absent for a Codex child. */
  callId?: string;
  /** Whose transcript it sits in: a subagent's id when nested, undefined for the session itself. */
  scope?: string;
  /** Its newest event's timestamp: how long it has been quiet. */
  lastAt?: string;
  /** Output tokens, once an end reports them. */
  tokens?: number;
}

function rec(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}
const earlier = (a: string | undefined, b: string | undefined) => (!a ? b : !b ? a : Date.parse(b) < Date.parse(a) ? b : a);

/** What an `Agent` call alone says about the subagent it launched. */
function fromCall(call: LocalSessionEvent, result: LocalSessionEvent | undefined, s: SessionSubagent) {
  const input = rec(call.tool?.input);
  const launched = rec(result?.tool?.result).status;
  if (!s.description && typeof input.description === "string") s.description = input.description;
  if (!s.type && typeof input.subagent_type === "string") s.type = input.subagent_type;
  s.startedAt = earlier(s.startedAt, call.ts);
  s.background ??= input.run_in_background === true || launched === "async_launched";
  if (s.state === "running" && result?.tool?.status === "error") s.state = "failed";
  s.callId = call.itemId;
  s.scope = call.subagent;
}

/**
 * The session's subagents, in the order they started, from the loaded window of
 * events: each one's own start/end markers and tagged rows, joined to the
 * parent's `Agent` call (which says what it was asked and whether it runs in
 * the background) and that call's result.
 */
export function sessionSubagents(events: readonly LocalSessionEvent[]): SessionSubagent[] {
  const calls = new Map<string, LocalSessionEvent>();
  const results = new Map<string, LocalSessionEvent>();
  const callOf = new Map<string, string>();
  const byId = new Map<string, SessionSubagent>();
  const asked = new Map<string, string>();
  const entry = (id: string) => {
    let s = byId.get(id);
    if (!s) byId.set(id, (s = { id, description: "", type: null, state: "running", background: null, toolUses: 0 }));
    return s;
  };
  for (const e of events) {
    if (e.kind === "tool" && e.role !== "tool" && e.tool?.name === "Agent" && e.itemId) calls.set(e.itemId, e);
    if (e.kind === "tool" && e.role === "tool" && e.parentId) {
      results.set(e.parentId, e);
      const agent = rec(e.tool?.result).agentId;
      if (typeof agent === "string") callOf.set(agent, e.parentId);
    }
    if (!e.subagent) continue;
    const s = entry(e.subagent);
    s.startedAt = earlier(s.startedAt, e.ts);
    if (e.ts && (!s.lastAt || Date.parse(e.ts) > Date.parse(s.lastAt))) s.lastAt = e.ts;
    if (!isSubagentMarker(e) && s.state !== "running" && e.ts && s.endedAt && Date.parse(e.ts) > Date.parse(s.endedAt)) {
      // Rows written after its end (not merely mirrored after it): it was resumed.
      s.state = "running";
      s.endedAt = s.durationMs = undefined;
    }
    if (e.kind === "text" && e.role === "user" && !asked.has(s.id)) asked.set(s.id, e.text.replace(/\s+/g, " ").trim().slice(0, 80));
    if (e.lifecycle === "subagent_start") {
      if (e.text) s.description = e.text;
      const type = rec(e.tool?.input).subagentType;
      if (typeof type === "string") s.type = type;
      if (e.parentId) callOf.set(s.id, e.parentId);
    } else if (e.lifecycle === "subagent_end") {
      const result = rec(e.tool?.result);
      s.state = e.tool?.status === "error" ? "failed" : "completed";
      s.endedAt = e.ts;
      if (typeof result.durationMs === "number") s.durationMs = result.durationMs;
      if (typeof result.tokens === "number") s.tokens = result.tokens;
      if (result.background === true) s.background = true;
      const said = rec(e.tool?.input).description;
      if (!s.description && typeof said === "string") s.description = said;
    } else if (e.kind === "tool" && e.role !== "tool") {
      s.toolUses += 1;
      s.lastStep = e;
    }
  }
  for (const s of byId.values()) {
    const call = calls.get(callOf.get(s.id) ?? "");
    if (call) fromCall(call, results.get(call.itemId!), s);
    // Never a raw id: what it was first asked, when nothing else named it.
    if (!s.description) s.description = asked.get(s.id) ?? "";
  }
  return [...byId.values()];
}

/** A subagent quiet this long while its session is not working is taken to have ended. */
export const SUBAGENT_QUIET_MS = 5 * 60_000;

/**
 * What to show a subagent as, given its session's status. One with no end is
 * "ended" once its session is gone (offline, ended), or idle with the subagent
 * quiet for SUBAGENT_QUIET_MS -- a background agent legitimately keeps working
 * while its parent idles, so a recent one stays running.
 */
export function subagentState(s: SessionSubagent, parent: string | undefined, now: number): SessionSubagent["state"] | "ended" {
  if (s.state !== "running") return s.state;
  if (parent === "busy" || parent === "waiting-approval") return "running";
  const quiet = !s.lastAt || now - Date.parse(s.lastAt) > SUBAGENT_QUIET_MS;
  return parent === "offline" || parent === "ended" || quiet ? "ended" : "running";
}

/** How long a subagent ran (or has been running, as of `now`); null when no clock says. */
export function subagentElapsedMs(s: SessionSubagent, now: number): number | null {
  if (s.durationMs != null) return s.durationMs;
  const start = s.startedAt ? Date.parse(s.startedAt) : NaN;
  const end = s.endedAt ? Date.parse(s.endedAt) : s.state === "running" ? now : NaN;
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
}

/** A clock the way Claude Code prints one: `42s`, `2m 14s` (or `2m`), `5h 19m`. */
export function sessionClock(ms: number | null, precise: boolean): string {
  if (ms == null) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return precise ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.round(s / 60)}m`;
  return `${Math.floor(s / 3600)}h ${Math.floor(s / 60) % 60}m`;
}

/** The muted tags after "Agent · <description>": its type, and "background" when it is. */
export function subagentTags(s: SessionSubagent): string[] {
  return [s.type ?? "", s.background ? "background" : ""].filter(Boolean);
}

/**
 * The `⎿` line under a subagent's row: live while it runs ("Running · 2m 14s ·
 * 7 tool uses · Bash(npm test)"), settled once it ends ("Done · 19m · 42 tool uses").
 */
export function subagentStatus(provider: LocalSessionProvider, s: SessionSubagent, now: number, parent?: string): string {
  const state = subagentState(s, parent, now);
  const running = state === "running";
  const uses = s.toolUses ? `${s.toolUses} tool use${s.toolUses === 1 ? "" : "s"}` : "";
  const step = running && s.lastStep ? toolHeader(provider, s.lastStep) : "";
  const said = { running: "Running", completed: "Done", failed: "Failed", ended: "Ended" }[state];
  // An end nobody reported has no honest clock.
  const took = state === "ended" ? "" : sessionClock(subagentElapsedMs(s, now), running);
  return [said, took, uses, step].filter(Boolean).join(" · ");
}

export interface StripRow { id: string; label: string; activity: string; elapsedMs: number | null; tokens?: number }

/**
 * The panel Claude Code pins above its prompt: `● main`, then one row per
 * subagent still running (its type, what it is doing now, its clock), five at
 * most with the rest counted.
 */
export function subagentStrip(provider: LocalSessionProvider, subs: readonly SessionSubagent[], parent: string | undefined, now: number, max = 5): { rows: StripRow[]; more: number } {
  const running = subs.filter(s => s.id && subagentState(s, parent, now) === "running");
  const rows = running.slice(0, max).map(s => ({
    id: s.id,
    label: s.type || s.description || "subagent",
    activity: s.lastStep ? toolHeader(provider, s.lastStep) : s.description,
    elapsedMs: subagentElapsedMs(s, now),
    ...(s.tokens != null ? { tokens: s.tokens } : {}),
  }));
  return { rows, more: Math.max(0, running.length - max) };
}

/** Claude Code's `✻ Waiting for N background agents to finish`, while its turn is over; 0 when there is nothing to wait for. */
export function backgroundWaiting(subs: readonly SessionSubagent[], parent: string | undefined, now: number): number {
  return subs.filter(s => s.id && s.background && !s.scope && subagentState(s, parent, now) === "running").length;
}

/**
 * The rows one transcript draws: the session's own (`scope` undefined) or one
 * subagent's (its id), from the WHOLE event list. A subagent is drawn inline in
 * the transcript that launched it, at its `Agent` call (Claude) or its parent's
 * `started` activity (Codex), as one `agent` row that expands into its own
 * rows; a background one's end also draws where the parent was told.
 */
export function buildRows(events: readonly LocalSessionEvent[], scope?: string): TranscriptRow[] {
  const subs = sessionSubagents(events);
  const byId = new Map(subs.map(s => [s.id, s]));
  const byCall = new Map(subs.filter(s => s.callId).map(s => [s.callId!, s]));
  const results = new Map(events.filter(e => e.kind === "tool" && e.role === "tool" && e.parentId).map(e => [e.parentId!, e]));
  // Codex's own SubAgentActivity rows name the agent path its `started` marker carries; the inline row replaces them.
  const codexPaths = new Set(events.filter(e => e.lifecycle === "subagent_start" && !e.parentId && e.text).map(e => e.text));
  const agentCalls = new Set<string>();
  const placed = new Set<string>();
  const rows: TranscriptRow[] = [];
  let proseItem: string | undefined;
  let prevTs: string | undefined;
  for (const e of events) {
    const marker = isSubagentMarker(e) && !!e.subagent;
    const child = e.subagent ? byId.get(e.subagent) : undefined;
    // A subagent launched before this window still gets its row, where its first event lands.
    if (child && child.scope === scope && !placed.has(child.id)) {
      rows.push({ kind: "agent", seq: e.seq, subagent: child, ...(e.lifecycle === "subagent_start" ? {} : { earlier: true }) });
      placed.add(child.id);
      proseItem = undefined;
      if (e.lifecycle === "subagent_start") continue;
    }
    // A subagent's markers belong where it was launched; its own rows, in its own transcript.
    if ((marker ? child?.scope : e.subagent) !== scope) continue;
    if (marker) {
      const s = byId.get(e.subagent!)!;
      if (isBackgroundEnd(e)) {
        const ms = rec(e.tool?.result).durationMs;
        rows.push({ kind: "subagent", seq: e.seq, id: s.id, text: e.text, failed: e.tool?.status === "error", ...(typeof ms === "number" ? { ms } : {}) });
      } else if (e.lifecycle === "subagent_start" && !placed.has(s.id)) {
        rows.push({ kind: "agent", seq: e.seq, subagent: s });
        placed.add(s.id);
      }
      proseItem = undefined;
      continue;
    }
    if (e.kind === "tool" && e.role !== "tool" && e.tool?.name === "Agent" && e.itemId) {
      let s = byCall.get(e.itemId);
      if (!s) {
        // Older history, or a subagent not mirrored: the call and its result are all there is.
        const result = results.get(e.itemId);
        fromCall(e, result, (s = { id: "", description: "", type: null, state: "running", background: null, toolUses: 0 }));
        if (result?.tool?.status === "done" && rec(result.tool.result).status !== "async_launched") s.state = "completed";
      }
      rows.push({ kind: "agent", seq: e.seq, subagent: s });
      if (s.id) placed.add(s.id);
      agentCalls.add(e.itemId);
      proseItem = undefined;
      continue;
    }
    if (e.kind === "tool" && e.role === "tool" && e.parentId && agentCalls.has(e.parentId)) continue;
    if (e.kind === "tool" && e.tool?.name === "SubAgentActivity" && codexPaths.has(e.text)) continue;
    const last = rows.at(-1);
    const before = prevTs;
    // A turn's end closes the clock: the wait for the next prompt is never thinking time.
    if (e.lifecycle === "turn_end" || e.lifecycle === "turn_aborted") prevTs = undefined;
    else if (e.ts) prevTs = e.ts;
    if (e.kind === "text" && e.role === "system") {
      // Platform notices (an event the desktop could not mirror) read as meta, not prose.
      rows.push({ kind: "notice", seq: e.seq, text: e.text });
      proseItem = undefined;
    } else if (e.kind === "text" && e.role === "user") {
      // An absent optional may arrive as an explicit null; `"userId" in actor` is true for one.
      const who = e.actor && "userId" in e.actor && e.actor.userId != null ? e.actor.userId
        : e.actor && "agentId" in e.actor ? e.actor.agentId : null;
      rows.push({ kind: "prompt", seq: e.seq, text: e.text, who, ts: e.ts });
      proseItem = undefined;
    } else if (e.kind === "text") {
      if (last?.kind === "prose" && e.itemId && proseItem === e.itemId) last.text += e.text;
      else { rows.push({ kind: "prose", seq: e.seq, text: e.text, final: false }); proseItem = e.itemId; }
    } else if (e.kind === "thinking") {
      // A thought is written down when it ends, so Claude Code times it as the
      // gap since the message before it, and never counts more than ten minutes.
      const gap = e.ts && before ? Date.parse(e.ts) - Date.parse(before) : NaN;
      const ms = Number.isFinite(gap) && gap > 0 ? Math.min(gap, THOUGHT_CAP_MS) : 0;
      if (last?.kind === "thinking") {
        last.text = [last.text, e.text].filter(Boolean).join("\n");
        last.ms = (last.ms ?? 0) + ms;
      } else rows.push({ kind: "thinking", seq: e.seq, text: e.text, ts: e.ts, ms });
      proseItem = undefined;
    } else if (e.kind === "tool" && e.role === "tool") {
      const call = [...rows].reverse().find((r): r is Extract<TranscriptRow, { kind: "tool" }> => r.kind === "tool" && !!e.parentId && r.call.itemId === e.parentId);
      if (call) call.result = e; else rows.push({ kind: "tool", seq: e.seq, call: e });
      proseItem = undefined;
    } else if (e.kind === "tool") {
      rows.push({ kind: "tool", seq: e.seq, call: e });
      proseItem = undefined;
    } else if (e.kind === "lifecycle" && e.lifecycle) {
      if (e.lifecycle === "turn_end") {
        const prose = [...rows].reverse().find((r): r is Extract<TranscriptRow, { kind: "prose" }> => r.kind === "prose");
        if (prose) prose.final = true;
      }
      if (e.lifecycle !== "turn_end" || e.text) rows.push({ kind: "lifecycle", seq: e.seq, lifecycle: e.lifecycle, text: e.text });
      proseItem = undefined;
    }
  }
  return rows;
}

const EDIT_TOOLS = new Set(["Edit", "Write", "MultiEdit", "NotebookEdit", "FileChange"]);
const THOUGHT_CAP_MS = 600_000;

/** What a tool call was for, in the words each terminal folds its activity into. */
export type ToolActivity = "commit" | "search" | "read" | "list" | "mcp" | "shell" | "edit" | "other";

const READ_WORDS = new Set(["cat", "head", "tail", "less", "more", "bat", "nl"]);
const LIST_WORDS = new Set(["ls", "tree", "find", "fd"]);
const SEARCH_WORDS = new Set(["grep", "rg", "ag", "ack"]);

/**
 * A shell command's purpose, from its first word. Each terminal decides this
 * with its own classifier, which nothing mirrors; this is the same call made
 * from the command text alone, so anything chained, piped or redirected is
 * just a command.
 */
function commandActivity(command: string): ToolActivity {
  const text = command.trim();
  if (/(^|&&|;)\s*git\s+(?:-\S+\s+\S+\s+)*commit\b/.test(text)) return "commit";
  if (!text || /[|;&<>`]/.test(text)) return "shell";
  const word = text.split(/\s+/)[0].split("/").pop() ?? "";
  if (READ_WORDS.has(word) || (word === "sed" && /\s-n\s/.test(text))) return "read";
  if (LIST_WORDS.has(word)) return "list";
  return SEARCH_WORDS.has(word) ? "search" : "shell";
}

/** Codex records its own verdict on each command; the desktop passes the first one through. */
const parsedCommand = (call: LocalSessionEvent) => {
  const parsed = rec(call.tool?.input).parsed;
  return rec(Array.isArray(parsed) ? parsed[0] : undefined);
};
const commandText = (call: LocalSessionEvent) => {
  const command = rec(call.tool?.input).command;
  return typeof command === "string" ? command : call.tool?.name === "Bash" ? call.text : "";
};

export function toolActivity(call: LocalSessionEvent): ToolActivity {
  const name = call.tool?.name ?? "";
  if (EDIT_TOOLS.has(name)) return "edit";
  if (name === "Read") return "read";
  if (name === "Grep" || name === "Glob") return "search";
  if (name === "LS") return "list";
  if (name.startsWith("mcp__")) return "mcp";
  if (name !== "Bash" && name !== "CommandExecution") return "other";
  const verdict = parsedCommand(call).type;
  if (verdict === "read") return "read";
  if (verdict === "list_files") return "list";
  if (verdict === "search") return "search";
  return commandActivity(commandText(call));
}

const rowStatus = (row: ToolTranscriptRow) => row.result?.tool?.status ?? row.call.tool?.status;
const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
const isCodexRow = (row: ToolTranscriptRow) => row.call.tool?.name === "CommandExecution";

/**
 * What a run folds into. Claude Code folds its searches, reads, listings,
 * shell commands and MCP calls (an edit is always drawn, with its diff, and so
 * is any other tool); Codex folds only what it calls exploring, and draws each
 * other command as its own "Ran …" row.
 */
function folds(row: TranscriptRow): row is ToolTranscriptRow {
  // A result with no call beside it is drawn as "Output", never counted as activity.
  if (row.kind !== "tool" || row.call.role === "tool") return false;
  const activity = toolActivity(row.call);
  if (activity === "edit" || activity === "other") return false;
  return !isCodexRow(row) || activity === "read" || activity === "list" || activity === "search";
}

/** The short hash `git commit` prints: "[main 5946ee6] message". */
function commitHash(row: ToolTranscriptRow): string {
  const output = fullOutput(row.result?.tool?.result ?? row.call.tool?.result) || (row.result?.text ?? "");
  return output.match(/\[[^\]\n]*?\b([0-9a-f]{7,40})\]/)?.[1] ?? "";
}

/** Codex's line under "Explored": "Read app.py", "List ls -la", "Search foo in src". */
function exploredLine(row: ToolTranscriptRow): string {
  const parsed = parsedCommand(row.call);
  const text = (key: string) => (typeof parsed[key] === "string" && parsed[key] ? (parsed[key] as string) : "");
  const command = commandText(row.call);
  const exit = rec(row.call.tool?.result).exitCode;
  const failed = rowStatus(row) === "error" ? ` (exit ${typeof exit === "number" ? exit : 1})` : "";
  switch (toolActivity(row.call)) {
    case "read": return `Read ${text("name") || text("path") || command.split(/\s+/).at(-1) || command}${failed}`;
    case "search": return `Search ${text("query") || command}${text("query") && text("path") ? ` in ${text("path")}` : ""}${failed}`;
    default: return `List ${command}${failed}`;
  }
}

/**
 * The one line a folded run collapses to, in its terminal's own words. Claude
 * Code: "Thought for 3s, committed 5946ee6, searched for 1 pattern, read 1
 * file, called github 2 times, ran 4 shell commands" -- always that order, a
 * file read twice counted once, a command that did not fail counted among the
 * shell commands whatever else it also was. Codex: "Explored", and how many of
 * them failed.
 */
export function toolGroupSummary(rows: readonly TranscriptRow[]): string {
  const tools = rows.filter((r): r is ToolTranscriptRow => r.kind === "tool");
  if (tools.some(isCodexRow)) {
    const failed = tools.filter(r => rowStatus(r) === "error").length;
    return failed ? `Explored · ${failed} failed` : "Explored";
  }
  const of = (activity: ToolActivity) => tools.filter(r => toolActivity(r.call) === activity);
  const thoughts = rows.filter((r): r is ThinkingTranscriptRow => r.kind === "thinking");
  const thought = thoughts.reduce((sum, r) => sum + (r.ms ?? 0), 0);
  const commits = of("commit").filter(r => rowStatus(r) !== "error");
  const files = new Set(of("read").map(r => rec(r.call.tool?.input).file_path).filter(path => typeof path === "string" && path));
  const read = files.size || of("read").length;
  const servers = [...new Set(of("mcp").map(r => r.call.tool?.name?.split("__")[1]).filter(Boolean))];
  const shells = tools.filter(r => r.call.tool?.name === "Bash");
  const ran = shells.filter(r => rowStatus(r) !== "error").length;
  const parts = [
    // Never less than a second, as the terminal draws it.
    thoughts.length ? `thought for ${formatMs(String(Math.max(1000, thought)))}` : "",
    commits.length === 1 ? `committed ${commitHash(commits[0]) || "changes"}` : commits.length ? `committed ${commits.length} times` : "",
    of("search").length ? `searched for ${count(of("search").length, "pattern", "patterns")}` : "",
    read ? `read ${count(read, "file", "files")}` : "",
    of("list").length ? `listed ${count(of("list").length, "directory", "directories")}` : "",
    of("mcp").length ? `called ${servers.join(", ") || "MCP"}${of("mcp").length > 1 ? ` ${of("mcp").length} times` : ""}` : "",
    ran ? `ran ${count(ran, "shell command", "shell commands")}` : "",
  ].filter(Boolean);
  // Nothing but failed commands: still say what ran.
  const line = parts.join(", ") || `ran ${count(shells.length || tools.length, "shell command", "shell commands")}`;
  return line[0].toUpperCase() + line.slice(1);
}

/**
 * Folds each run of consecutive tool calls its terminal would fold into one
 * summary item, so a turn reads as prompt → "Thought for 3s, read 3 files,
 * ran 2 shell commands" → the edit → answer. Claude Code folds a thought in
 * with the calls around it, and a thought on its own is still a line; Codex
 * draws a thought only when it wrote a summary of it, as a row of its own.
 * Edits, prompts, prose, notices and lifecycle lines are never folded. While
 * `live` (the turn is still running), a call or thought that is the last row
 * stays unfolded so its progress stays visible.
 */
export function groupRows(rows: readonly TranscriptRow[], live: boolean, provider: LocalSessionProvider = "claude"): TranscriptItem[] {
  const out: TranscriptItem[] = [];
  const codex = provider === "codex";
  const tail = rows.at(-1)?.kind;
  const liveRow = live && (tail === "tool" || tail === "thinking") ? rows.length - 1 : -1;
  const joins = (i: number) => {
    const row = rows[i];
    if (i === liveRow) return false;
    // A thought with words to show keeps its own row, where they can be opened.
    return row.kind === "thinking" ? !codex && !row.text.trim() : folds(row) && (!codex || isCodexRow(row));
  };
  let i = 0;
  while (i < rows.length) {
    const first = rows[i];
    if (codex && first.kind === "thinking" && !first.text.trim()) { i++; continue; }
    if (!joins(i)) { out.push(first); i++; continue; }
    let end = i;
    const terminal = (row: TranscriptRow) => (row.kind === "tool" ? isCodexRow(row) : codex);
    while (end + 1 < rows.length && joins(end + 1) && terminal(rows[end + 1]) === terminal(first)) end++;
    const run = rows.slice(i, end + 1) as (ToolTranscriptRow | ThinkingTranscriptRow)[];
    const calls = run.filter((r): r is ToolTranscriptRow => r.kind === "tool");
    out.push({ kind: "tools", seq: first.seq, rows: run, summary: toolGroupSummary(run), ...(calls.some(isCodexRow) ? { lines: calls.map(exploredLine) } : {}) });
    i = end + 1;
  }
  return out;
}

/** The one collapsed line a thinking block shows: live, timed, or neither. */
export function thinkingLabel(row: ThinkingTranscriptRow, live: boolean): string {
  if (live) return "Thinking…";
  const took = row.ms != null ? formatMs(String(row.ms)) : "";
  return took && took !== "0s" ? `Thought for ${took}` : "Thought";
}

/** "Agent "Fix login" finished · 19m": the line a background subagent's end draws in its parent. */
export function subagentLabel(row: Extract<TranscriptRow, { kind: "subagent" }>): string {
  const took = sessionClock(row.ms ?? null, false);
  return took && took !== "0s" ? `${row.text} · ${took}` : row.text;
}

/**
 * `subagentName` resolves a subagent id to what it was asked to do, for a hand-back's
 * "Message from @…" line (the terminal's own wording); without one (or for an id
 * it does not know) the line says "@subagent".
 */
export function toolHeader(provider: LocalSessionProvider, call: LocalSessionEvent, subagentName?: (subagent: string) => string | undefined): string {
  if (call.tool?.name === "AgentReport") {
    const agent = (call.tool.input as { agentId?: unknown } | undefined)?.agentId;
    return `Message from @${(typeof agent === "string" && subagentName?.(agent)) || "subagent"}`;
  }
  // A tool-result event with no matching call (the pairing in buildRows never
  // found its parent) is drawn as its own row — there is no call-side name or
  // args to summarize, so name it for what it is rather than printing
  // "result(...)" or the adapter's internal result-item name.
  if (call.role === "tool") return "Output";
  const name = call.tool?.name ?? "Tool";
  // Claude Code draws its Edit tool as "Update(path)".
  if (provider === "claude") return `${name === "Edit" || name === "MultiEdit" ? "Update" : name}(${call.text})`;
  switch (name) {
    case "CommandExecution": {
      // Codex puts the output preview in `text`; the command itself rides in tool.input.
      const input = call.tool?.input as { command?: unknown } | undefined;
      return `Ran ${typeof input?.command === "string" ? input.command : call.text}`;
    }
    case "FileChange": return fileChangeHeader(call);
    case "McpToolCall": return `Called ${call.text}`;
    case "web.search": return `Searched web: ${call.text}`;
    case "SubAgentActivity": case "CollabAgentToolCall": return `Agent ${call.text}`;
    default: return `${name} ${call.text}`.trim();
  }
}

/**
 * Full output text for the expanded view, from whichever shape the adapter
 * sent. Recognizes every result shape the Claude and Codex adapters emit
 * (command stdout/stderr, a plain output/content string or content-block
 * array, a Task-style {description,status} summary, and a Codex FileChange
 * per-file {content} map). Any other shape (e.g. Claude's Read {numLines})
 * carries nothing this component can expand into — return "" rather than a
 * JSON dump nobody asked to read.
 */
export function fullOutput(result: unknown): string {
  if (!result || typeof result !== "object") return "";
  const r = result as Record<string, unknown>;
  if (typeof r.stdout === "string" || typeof r.stderr === "string") return [r.stdout, r.stderr].filter((s): s is string => typeof s === "string" && s.length > 0).join("\n");
  if (typeof r.output === "string") return r.output;
  if (typeof r.content === "string") return r.content;
  if (Array.isArray(r.content)) return r.content.map(b => (b && typeof b === "object" && "text" in b ? String((b as { text: unknown }).text) : "")).join("\n");
  if (typeof r.description === "string" && typeof r.status === "string") return `${r.description}\n${r.status}`;
  const files = Object.values(r);
  if (files.length > 0 && files.every(f => f && typeof f === "object" && typeof (f as { content?: unknown }).content === "string")) {
    return files.map(f => (f as { content: string }).content).join("\n\n");
  }
  return "";
}

/** Unified-diff lines from Claude's structuredPatch hunks or Codex FileChange diffs. */
export function diffLines(result: unknown): string[] {
  if (!result || typeof result !== "object") return [];
  const r = result as Record<string, unknown>;
  if (Array.isArray(r.structuredPatch)) {
    return r.structuredPatch.flatMap(h => {
      const lines = h && typeof h === "object" ? (h as { lines?: unknown }).lines : undefined;
      return Array.isArray(lines) ? lines.filter((l): l is string => typeof l === "string") : [];
    });
  }
  return Object.values(r).flatMap(change => (change && typeof change === "object" && typeof (change as { diff?: unknown }).diff === "string" ? (change as { diff: string }).diff.split("\n") : []));
}

/** One line of a diff as a terminal prints it: its number, its mark, its text. */
export interface DiffLine { n: number | null; mark: "+" | "-" | " "; text: string }

const markOf = (line: string): DiffLine["mark"] => (line[0] === "+" || line[0] === "-" ? line[0] : " ");

/** A hunk's lines with the numbers beside them: a removed line keeps its old number, every other its new one. */
function numberHunk(lines: readonly string[], oldStart: number, newStart: number): DiffLine[] {
  let old = oldStart;
  let fresh = newStart;
  return lines.map(line => {
    // "\ No newline at end of file" is a note about the line above, not a line.
    if (line.startsWith("\\")) return { n: null, mark: " ", text: line };
    const mark = markOf(line);
    const n = mark === "-" ? old++ : fresh++;
    if (mark === " ") old++;
    return { n, mark, text: stripAnsi(line.slice(1)) };
  });
}

/** A unified diff, numbered from its own "@@ -a,b +c,d @@" headers. */
function numberUnified(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let hunk: string[] = [];
  let start: [number, number] | null = null;
  const flush = () => { if (start) out.push(...numberHunk(hunk, start[0], start[1])); hunk = []; };
  for (const line of diff.replace(/\n$/, "").split("\n")) {
    const header = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (header) { flush(); start = [Number(header[1]), Number(header[2])]; }
    else if (start) hunk.push(line);
    // Before the first hunk: a file header, or a diff that carries no numbers at all.
    else if (!line.startsWith("+++") && !line.startsWith("---")) out.push({ n: null, mark: markOf(line), text: stripAnsi(line.slice(1)) });
  }
  flush();
  return out;
}

/**
 * The diff of an edit with its line numbers: Claude's structuredPatch hunks
 * (context included, as Claude Code draws them) or a Codex FileChange (the
 * changed lines only, as Codex draws them; a new or deleted file is its content).
 */
export function numberedDiff(result: unknown): DiffLine[] {
  const r = rec(result);
  if (Array.isArray(r.structuredPatch)) {
    return r.structuredPatch.flatMap(h => {
      const hunk = rec(h);
      const lines = Array.isArray(hunk.lines) ? hunk.lines.filter((l): l is string => typeof l === "string") : [];
      return numberHunk(lines, typeof hunk.oldStart === "number" ? hunk.oldStart : 1, typeof hunk.newStart === "number" ? hunk.newStart : 1);
    });
  }
  return Object.values(r).flatMap((change): DiffLine[] => {
    const c = rec(change);
    if (typeof c.diff === "string" && c.diff) return numberUnified(c.diff).filter(line => line.mark !== " ");
    const content = typeof c.content === "string" ? c.content.replace(/\n$/, "") : "";
    const mark = c.type === "delete" ? "-" : "+";
    return content ? content.split("\n").map((text, i) => ({ n: i + 1, mark, text: stripAnsi(text) })) : [];
  });
}

/** The diff a call's own input shows before (or without) its result: Edit/MultiEdit old→new, Write's content. */
export function inputDiff(name: string, input: unknown): DiffLine[] {
  const i = rec(input);
  const side = (text: unknown, mark: "+" | "-"): DiffLine[] => typeof text === "string" ? text.split("\n").map(t => ({ n: null, mark, text: t })) : [];
  const pair = (o: unknown, n: unknown) => [...side(o, "-"), ...side(n, "+")];
  if (name === "Edit") return pair(i.old_string, i.new_string);
  if (name === "MultiEdit" && Array.isArray(i.edits)) return i.edits.flatMap(e => pair(rec(e).old_string, rec(e).new_string));
  if (name === "Write") return side(i.content, "+");
  return [];
}

/**
 * How an approval card draws a call's input. A friendly view exists only when it
 * renders EVERY key of the input (an allow is bound to the hash of all of it);
 * any other key, or a value of an unexpected type, falls back to the whole JSON.
 */
export type ApprovalInputView =
  | { kind: "json"; text: string }
  /** `notes`: description, background, timeout; `outsideSandbox`: dangerouslyDisableSandbox is true. */
  | { kind: "bash"; command: string; outsideSandbox: boolean; notes: string[] }
  /** One entry per edit (Write is one all-"+" edit); `everyOccurrence` is that edit's replace_all. */
  | { kind: "edit"; path: string; edits: { lines: DiffLine[]; everyOccurrence: boolean }[] };

const plain = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const onlyKeys = (o: Record<string, unknown>, keys: readonly string[]) => Object.keys(o).every(k => keys.includes(k));
const isOpt = (v: unknown, type: "string" | "boolean" | "number") => v === undefined || typeof v === type;
const isStr = (v: unknown): v is string => typeof v === "string";
const lined = (text: string, mark: "+" | "-"): DiffLine[] => text.split("\n").map(t => ({ n: null, mark, text: t }));
const editOf = (e: Record<string, unknown>) => ({ lines: [...lined(e.old_string as string, "-"), ...lined(e.new_string as string, "+")], everyOccurrence: e.replace_all === true });
const isEdit = (e: unknown): e is Record<string, unknown> => plain(e) && onlyKeys(e, ["old_string", "new_string", "replace_all"])
  && isStr(e.old_string) && isStr(e.new_string) && isOpt(e.replace_all, "boolean");

export function approvalInputView(tool: string, input: Record<string, unknown>): ApprovalInputView {
  const i = input;
  if (tool === "Bash" && onlyKeys(i, ["command", "description", "run_in_background", "timeout", "dangerouslyDisableSandbox"])
    && isStr(i.command) && isOpt(i.description, "string") && isOpt(i.run_in_background, "boolean") && isOpt(i.timeout, "number")
    && isOpt(i.dangerouslyDisableSandbox, "boolean")) {
    const notes = [
      isStr(i.description) ? `Description: ${i.description}` : "",
      i.run_in_background === true ? "Runs in the background" : "",
      typeof i.timeout === "number" ? `Timeout: ${i.timeout} ms` : "",
    ].filter(Boolean);
    return { kind: "bash", command: i.command, outsideSandbox: i.dangerouslyDisableSandbox === true, notes };
  }
  if (tool === "Edit" && onlyKeys(i, ["file_path", "old_string", "new_string", "replace_all"]) && isStr(i.file_path)
    && isStr(i.old_string) && isStr(i.new_string) && isOpt(i.replace_all, "boolean")) {
    return { kind: "edit", path: i.file_path, edits: [editOf(i)] };
  }
  if (tool === "MultiEdit" && onlyKeys(i, ["file_path", "edits"]) && isStr(i.file_path) && Array.isArray(i.edits) && i.edits.every(isEdit)) {
    return { kind: "edit", path: i.file_path, edits: i.edits.map(editOf) };
  }
  if (tool === "Write" && onlyKeys(i, ["file_path", "content"]) && isStr(i.file_path) && isStr(i.content)) {
    return { kind: "edit", path: i.file_path, edits: [{ lines: lined(i.content, "+"), everyOccurrence: false }] };
  }
  return { kind: "json", text: JSON.stringify(input, null, 2) };
}

const sortKeys = (v: unknown): unknown => Array.isArray(v) ? v.map(sortKeys)
  : v && typeof v === "object" ? Object.fromEntries(Object.keys(v).sort().map(k => [k, sortKeys((v as Record<string, unknown>)[k])])) : v;
/** Compact JSON with keys sorted at every level: what the Mac digests (`lifecycle::canonical` + serde_json compact). */
export const canonicalJson = (v: unknown): string => JSON.stringify(sortKeys(v));

const tally = (lines: readonly DiffLine[]) => ({
  added: lines.filter(l => l.mark === "+").length,
  removed: lines.filter(l => l.mark === "-").length,
});

/** Codex's header for a patch: "Edited app.py (+1 -1)", "Added notes.md (+3 -0)", "Edited 3 files (+9 -2)". */
function fileChangeHeader(call: LocalSessionEvent): string {
  const changes = Object.entries(rec(call.tool?.result));
  if (!changes.length) return `Edited ${call.text}`;
  const { added, removed } = tally(numberedDiff(call.tool?.result));
  if (changes.length > 1) return `Edited ${changes.length} files (+${added} -${removed})`;
  const kind = rec(changes[0][1]).type;
  return `${kind === "add" ? "Added" : kind === "delete" ? "Deleted" : "Edited"} ${changes[0][0]} (+${added} -${removed})`;
}

/** Claude Code's line under an edit: "Added 1 line, removed 1 line", or "Wrote 3 lines to notes.md" for a new file. */
function editSummary(row: ToolTranscriptRow, lines: readonly DiffLine[]): string {
  const name = row.call.tool?.name ?? "";
  if (!EDIT_TOOLS.has(name) || name === "FileChange" || rowStatus(row) !== "done") return "";
  const { added, removed } = tally(lines);
  if (!added && !removed) {
    const wrote = rec(row.call.tool?.input).contentLines;
    return name === "Write" && typeof wrote === "number" ? `Wrote ${count(wrote, "line", "lines")} to ${row.call.text}` : "";
  }
  const said = [added ? `added ${count(added, "line", "lines")}` : "", removed ? `removed ${count(removed, "line", "lines")}` : ""].filter(Boolean).join(", ");
  return said[0].toUpperCase() + said.slice(1);
}

export interface ToolRowView {
  status: "running" | "done" | "error";
  /** The harness's own output preview (already ANSI-stripped); "" when the row's text is a label. */
  preview: string;
  /** Everything the adapter recorded, for the expanded view; "" when there is nothing to expand into. */
  full: string;
  diff: string[];
  /** The same diff with the line numbers its terminal prints beside it. */
  numbered: DiffLine[];
  /** The line the terminal prints under an edit ("Added 1 line, removed 1 line"); "" when it prints none. */
  summary: string;
  /** Output lines beyond the preview; 0 when unknown. */
  more: number;
  /** Offer "show output" even though no line count says more exists. */
  showOutput: boolean;
  truncated: boolean;
}

/** What both renderers draw for a tool row — one computation, two skins. */
export function toolRowView(row: ToolTranscriptRow): ToolRowView {
  const status = row.result?.tool?.status ?? row.call.tool?.status ?? "running";
  const resultPayload = row.result?.tool?.result ?? row.call.tool?.result;
  const resultNumbered = numberedDiff(resultPayload);
  // A running call (or one whose result carries no patch) shows the diff its own input describes.
  const numbered = resultNumbered.length ? resultNumbered : inputDiff(row.call.tool?.name ?? "", row.call.tool?.input);
  const diff = resultNumbered.length ? diffLines(resultPayload).map(stripAnsi) : numbered.map(l => l.mark + l.text);
  // tool.lines is set exactly when `text` is an output preview (Claude
  // tool_result rows and Codex CommandExecution); it's absent when `text` is
  // a label (McpToolCall, FileChange, SubAgentActivity, ...) — so a label
  // never gets shown as if it were output.
  const rawPreview = stripAnsi(row.result?.text ?? (row.call.tool?.lines != null ? row.call.text : ""));
  const recorded = stripAnsi(fullOutput(resultPayload));
  // Codex prints the END of a command's output, Claude Code the beginning. The
  // end is only known from the whole output, not from a preview of its start.
  const fromEnd = row.call.tool?.name === "CommandExecution";
  const lines = fromEnd && recorded ? recorded.replace(/\n+$/, "").split("\n") : rawPreview ? rawPreview.split("\n") : [];
  const truncated = (row.result ?? row.call).truncated;
  const reported = row.result?.tool?.lines ?? row.call.tool?.lines;
  // The desktop bounds a big output to a prefix ending in "…" (bound_json), and a
  // preview is only the first lines: either way the real end is missing, so show
  // the head and let the line count say how much more there is.
  const cut = (reported != null && reported > lines.length) || (!!truncated && (lines[lines.length - 1] ?? "").endsWith("…"));
  const tail = fromEnd && !cut;
  // Clamp the preview; a clamped preview with no richer output expands into itself.
  const clamped = lines.length > PREVIEW_LINES;
  const preview = (tail ? lines.slice(-PREVIEW_LINES) : lines.slice(0, PREVIEW_LINES)).join("\n");
  const full = recorded || (clamped ? lines.join("\n") : "");
  const shownLines = preview ? Math.min(lines.length, PREVIEW_LINES) : 0;
  const counted = reported ?? (clamped ? lines.length : undefined);
  const total = counted != null && fromEnd ? Math.max(counted, lines.length) : counted;
  const more = full && total != null ? Math.max(0, total - shownLines) : 0;
  const showOutput = more === 0 && full.length > preview.length;
  return { status, preview, full, diff, numbered, summary: editSummary(row, resultNumbered), more, showOutput, truncated };
}

function formatMs(text: string): string {
  const ms = Number(text);
  if (!Number.isFinite(ms) || ms <= 0) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** Meta-line copy for a lifecycle row; "" means draw nothing. */
export function lifecycleLabel(row: Extract<TranscriptRow, { kind: "lifecycle" }>): string {
  switch (row.lifecycle) {
    case "turn_end": {
      const worked = formatMs(row.text);
      return worked ? `Worked for ${worked}` : "";
    }
    case "turn_aborted": return "Interrupted";
    case "compaction": return "Context compacted";
    // Matches the session-status label, and carries the reason the desktop's
    // hook captured — Claude writes neither a pending question nor a
    // permission prompt to its transcript, so this row is the only record.
    case "approval_pending": return row.text ? `Waiting for input: ${row.text}` : "Waiting for input";
    default: return "Turn started";
  }
}

/** The reason the session is waiting on its owner — the latest lifecycle event, when it is approval_pending; null otherwise ("" when no reason was captured). */
export function pendingApproval(events: readonly LocalSessionEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "lifecycle" && !e.subagent) return e.lifecycle === "approval_pending" ? e.text ?? "" : null;
  }
  return null;
}
