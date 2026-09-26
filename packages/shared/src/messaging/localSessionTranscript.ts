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
  /** `ts` is when thinking began; `ms` is how long it ran, once a later event with a timestamp closes it. */
  | { kind: "thinking"; seq: number; text: string; ts?: string; ms?: number }
  | { kind: "tool"; seq: number; call: LocalSessionEvent; result?: LocalSessionEvent }
  | { kind: "notice"; seq: number; text: string }
  | { kind: "lifecycle"; seq: number; lifecycle: NonNullable<LocalSessionEvent["lifecycle"]>; text: string }
  /** A background subagent's end, as its parent saw it: one line that points at its row. */
  | { kind: "subagent"; seq: number; id: string; text: string; failed: boolean; ms?: number }
  /** A subagent, inline where it was launched; it expands into `buildRows(events, subagent.id)`. */
  | { kind: "agent"; seq: number; subagent: SessionSubagent };
export type ToolTranscriptRow = Extract<TranscriptRow, { kind: "tool" }>;
export type ThinkingTranscriptRow = Extract<TranscriptRow, { kind: "thinking" }>;
/** A folded run of tool calls (and the thinking between them), drawn as one summary line. */
export type ToolGroupItem = { kind: "tools"; seq: number; rows: (ToolTranscriptRow | ThinkingTranscriptRow)[]; summary: string };
export type TranscriptItem = TranscriptRow | ToolGroupItem;

/** Tool output and diffs show this many lines until the reader expands them. */
export const PREVIEW_LINES = 3;

/** Each TUI's own glyphs; renderers draw exactly these. */
export const GLYPH = {
  claude: { prompt: ">", bullet: "⏺", output: "⎿", thinking: "✻", working: "✻" },
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
      if (result.background === true) s.background = true;
    } else if (e.kind === "tool" && e.role !== "tool") {
      s.toolUses += 1;
      s.lastStep = e;
    }
  }
  for (const s of byId.values()) {
    const call = calls.get(callOf.get(s.id) ?? "");
    if (call) fromCall(call, results.get(call.itemId!), s);
  }
  return [...byId.values()];
}

/** How long a subagent ran (or has been running, as of `now`); null when no clock says. */
export function subagentElapsedMs(s: SessionSubagent, now: number): number | null {
  if (s.durationMs != null) return s.durationMs;
  const start = s.startedAt ? Date.parse(s.startedAt) : NaN;
  const end = s.endedAt ? Date.parse(s.endedAt) : s.state === "running" ? now : NaN;
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
}

function clock(ms: number | null, precise: boolean): string {
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
export function subagentStatus(provider: LocalSessionProvider, s: SessionSubagent, now: number): string {
  const running = s.state === "running";
  const uses = s.toolUses ? `${s.toolUses} tool use${s.toolUses === 1 ? "" : "s"}` : "";
  const step = running && s.lastStep ? toolHeader(provider, s.lastStep) : "";
  const said = running ? "Running" : s.state === "failed" ? "Failed" : "Done";
  return [said, clock(subagentElapsedMs(s, now), running), uses, step].filter(Boolean).join(" · ");
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
  for (const e of events) {
    const marker = isSubagentMarker(e) && !!e.subagent;
    // A subagent's markers belong where it was launched; its own rows, in its own transcript.
    if ((marker ? byId.get(e.subagent!)?.scope : e.subagent) !== scope) continue;
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
    // The next timestamped event closes a thinking block — that gap is how long it thought.
    if (last?.kind === "thinking" && e.kind !== "thinking" && last.ts && e.ts) {
      const ms = Date.parse(e.ts) - Date.parse(last.ts);
      if (Number.isFinite(ms) && ms >= 0) last.ms = ms;
    }
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
      if (last?.kind === "thinking") last.text = [last.text, e.text].filter(Boolean).join("\n");
      else rows.push({ kind: "thinking", seq: e.seq, text: e.text, ts: e.ts });
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

/** "Ran 7 tools · 2 edits · 1 failed" — the one line a folded run collapses to. */
export function toolGroupSummary(rows: readonly TranscriptRow[]): string {
  const tools = rows.filter((r): r is ToolTranscriptRow => r.kind === "tool");
  const edits = tools.filter(r => EDIT_TOOLS.has(r.call.tool?.name ?? "")).length;
  const failed = tools.filter(r => (r.result?.tool?.status ?? r.call.tool?.status) === "error").length;
  const n = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;
  return [`Ran ${n(tools.length, "tool")}`, edits ? n(edits, "edit") : "", failed ? `${failed} failed` : ""].filter(Boolean).join(" · ");
}

/**
 * Folds each run of two or more consecutive tool calls into one summary item,
 * so a turn reads as prompt → "Ran 7 tools" → answer. Thinking that sits
 * between two tools of a run folds with it; thinking before the first or after
 * the last stays outside. Prompts, prose, notices and lifecycle lines are
 * never folded. While `live` (the turn is still running), a tool that is the
 * last row stays unfolded so its progress stays visible.
 */
export function groupRows(rows: readonly TranscriptRow[], live: boolean): TranscriptItem[] {
  const out: TranscriptItem[] = [];
  const liveTool = live && rows.at(-1)?.kind === "tool" ? rows.length - 1 : -1;
  let i = 0;
  while (i < rows.length) {
    if (rows[i].kind !== "tool" || i === liveTool) { out.push(rows[i]); i++; continue; }
    // Extend over tools and thinking, then trim back to the last tool.
    let end = i;
    for (let j = i; j < rows.length && j !== liveTool && (rows[j].kind === "tool" || rows[j].kind === "thinking"); j++) {
      if (rows[j].kind === "tool") end = j;
    }
    const run = rows.slice(i, end + 1) as (ToolTranscriptRow | ThinkingTranscriptRow)[];
    if (run.filter(r => r.kind === "tool").length < 2) out.push(...run);
    else out.push({ kind: "tools", seq: run[0].seq, rows: run, summary: toolGroupSummary(run) });
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
  const took = clock(row.ms ?? null, false);
  return took && took !== "0s" ? `${row.text} · ${took}` : row.text;
}

/**
 * `subagentName` resolves a subagent id to what it was asked to do, for a hand-back's
 * "Report from …" line; without one (or for an id it does not know) the line
 * says "a subagent".
 */
export function toolHeader(provider: LocalSessionProvider, call: LocalSessionEvent, subagentName?: (subagent: string) => string | undefined): string {
  if (call.tool?.name === "AgentReport") {
    const agent = (call.tool.input as { agentId?: unknown } | undefined)?.agentId;
    return `Report from ${(typeof agent === "string" && subagentName?.(agent)) || "a subagent"}`;
  }
  // A tool-result event with no matching call (the pairing in buildRows never
  // found its parent) is drawn as its own row — there is no call-side name or
  // args to summarize, so name it for what it is rather than printing
  // "result(...)" or the adapter's internal result-item name.
  if (call.role === "tool") return "Output";
  const name = call.tool?.name ?? "Tool";
  if (provider === "claude") return `${name}(${call.text})`;
  switch (name) {
    case "CommandExecution": {
      // Codex puts the output preview in `text`; the command itself rides in tool.input.
      const input = call.tool?.input as { command?: unknown } | undefined;
      return `Ran ${typeof input?.command === "string" ? input.command : call.text}`;
    }
    case "FileChange": return `Edited ${call.text}`;
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

export interface ToolRowView {
  status: "running" | "done" | "error";
  /** The harness's own output preview (already ANSI-stripped); "" when the row's text is a label. */
  preview: string;
  /** Everything the adapter recorded, for the expanded view; "" when there is nothing to expand into. */
  full: string;
  diff: string[];
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
  const diff = diffLines(resultPayload).map(stripAnsi);
  // tool.lines is set exactly when `text` is an output preview (Claude
  // tool_result rows and Codex CommandExecution); it's absent when `text` is
  // a label (McpToolCall, FileChange, SubAgentActivity, ...) — so a label
  // never gets shown as if it were output.
  const rawPreview = stripAnsi(row.result?.text ?? (row.call.tool?.lines != null ? row.call.text : ""));
  // Clamp the preview; a clamped preview with no richer output expands into itself.
  const rawLines = rawPreview ? rawPreview.split("\n") : [];
  const clamped = rawLines.length > PREVIEW_LINES;
  const preview = rawLines.slice(0, PREVIEW_LINES).join("\n");
  const full = stripAnsi(fullOutput(resultPayload)) || (clamped ? rawPreview : "");
  const shownLines = preview ? Math.min(rawLines.length, PREVIEW_LINES) : 0;
  const total = row.result?.tool?.lines ?? row.call.tool?.lines ?? (clamped ? rawLines.length : undefined);
  const more = full && total != null ? Math.max(0, total - shownLines) : 0;
  const showOutput = more === 0 && full.length > preview.length;
  const truncated = (row.result ?? row.call).truncated;
  return { status, preview, full, diff, more, showOutput, truncated };
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
