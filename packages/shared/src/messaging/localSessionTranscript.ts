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
  | { kind: "thinking"; seq: number; text: string }
  | { kind: "tool"; seq: number; call: LocalSessionEvent; result?: LocalSessionEvent }
  | { kind: "notice"; seq: number; text: string }
  | { kind: "lifecycle"; seq: number; lifecycle: NonNullable<LocalSessionEvent["lifecycle"]>; text: string };
export type ToolTranscriptRow = Extract<TranscriptRow, { kind: "tool" }>;

/** Each TUI's own glyphs; renderers draw exactly these. */
export const GLYPH = {
  claude: { prompt: ">", bullet: "⏺", output: "⎿", thinking: "✻", working: "✻" },
  codex: { prompt: "›", bullet: "•", output: "└", thinking: "•", working: "•" },
} as const;

// Strips terminal escape sequences (color codes, cursor moves) that ride
// along in raw tool output — they render as garbage in a <pre>.
const ANSI_RE = /\[[0-9;?]*[ -/]*[@-~]/g;
export function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, "");
}

export function buildRows(events: readonly LocalSessionEvent[]): TranscriptRow[] {
  const rows: TranscriptRow[] = [];
  let proseItem: string | undefined;
  for (const e of events) {
    const last = rows.at(-1);
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
      else rows.push({ kind: "thinking", seq: e.seq, text: e.text });
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

export function toolHeader(provider: LocalSessionProvider, call: LocalSessionEvent): string {
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
  const preview = stripAnsi(row.result?.text ?? (row.call.tool?.lines != null ? row.call.text : ""));
  const full = stripAnsi(fullOutput(resultPayload));
  const shownLines = preview ? preview.split("\n").length : 0;
  const total = row.result?.tool?.lines ?? row.call.tool?.lines;
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
    case "approval_pending": return "Waiting for approval on the owner's Mac";
    default: return "Turn started";
  }
}
