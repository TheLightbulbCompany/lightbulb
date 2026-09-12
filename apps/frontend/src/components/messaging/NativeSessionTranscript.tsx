"use client";

import { useEffect, useState } from "react";
import type { LocalSessionEvent } from "@lightbulb/shared";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "./roomMarkdown";

export type Provider = "claude" | "codex";
export type Person = { kind: "human" | "agent"; id: string; displayName: string };
export type Row =
  | { kind: "prompt"; seq: number; text: string; who: string | null; ts?: string }
  | { kind: "prose"; seq: number; text: string; final: boolean }
  | { kind: "thinking"; seq: number; text: string }
  | { kind: "tool"; seq: number; call: LocalSessionEvent; result?: LocalSessionEvent }
  | { kind: "lifecycle"; seq: number; lifecycle: NonNullable<LocalSessionEvent["lifecycle"]>; text: string };

const GLYPH = {
  claude: { prompt: ">", bullet: "⏺", output: "⎿", thinking: "✻", working: "✻" },
  codex: { prompt: "›", bullet: "•", output: "└", thinking: "•", working: "•" },
} as const;

// Strips terminal escape sequences (color codes, cursor moves) that ride
// along in raw tool output — they render as garbage in a <pre>.
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, "");
}

export function buildRows(events: readonly LocalSessionEvent[]): Row[] {
  const rows: Row[] = [];
  let proseItem: string | undefined;
  for (const e of events) {
    const last = rows.at(-1);
    if (e.kind === "text" && e.role === "user") {
      rows.push({ kind: "prompt", seq: e.seq, text: e.text, who: e.actor ? ("userId" in e.actor ? e.actor.userId : e.actor.agentId) : null, ts: e.ts });
      proseItem = undefined;
    } else if (e.kind === "text") {
      if (last?.kind === "prose" && e.itemId && proseItem === e.itemId) last.text += e.text;
      else { rows.push({ kind: "prose", seq: e.seq, text: e.text, final: false }); proseItem = e.itemId; }
    } else if (e.kind === "thinking") {
      if (last?.kind === "thinking") last.text = [last.text, e.text].filter(Boolean).join("\n");
      else rows.push({ kind: "thinking", seq: e.seq, text: e.text });
      proseItem = undefined;
    } else if (e.kind === "tool" && e.role === "tool") {
      const call = [...rows].reverse().find((r): r is Extract<Row, { kind: "tool" }> => r.kind === "tool" && !!e.parentId && r.call.itemId === e.parentId);
      if (call) call.result = e; else rows.push({ kind: "tool", seq: e.seq, call: e });
      proseItem = undefined;
    } else if (e.kind === "tool") {
      rows.push({ kind: "tool", seq: e.seq, call: e });
      proseItem = undefined;
    } else if (e.kind === "lifecycle" && e.lifecycle) {
      if (e.lifecycle === "turn_end") {
        const prose = [...rows].reverse().find((r): r is Extract<Row, { kind: "prose" }> => r.kind === "prose");
        if (prose) prose.final = true;
      }
      if (e.lifecycle !== "turn_end" || e.text) rows.push({ kind: "lifecycle", seq: e.seq, lifecycle: e.lifecycle, text: e.text });
      proseItem = undefined;
    }
  }
  return rows;
}

function toolHeader(provider: Provider, call: LocalSessionEvent): string {
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
function fullOutput(result: unknown): string {
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
function diffLines(result: unknown): string[] {
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

function ToolRow({ provider, row, seq }: { provider: Provider; row: Extract<Row, { kind: "tool" }>; seq: number }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const status = row.result?.tool?.status ?? row.call.tool?.status ?? "running";
  const resultPayload = row.result?.tool?.result ?? row.call.tool?.result;
  const diff = diffLines(resultPayload).map(stripAnsi);
  const isDiff = diff.length > 0;
  // tool.lines is set exactly when `text` is an output preview (Claude
  // tool_result rows and Codex CommandExecution); it's absent when `text` is
  // a label (McpToolCall, FileChange, SubAgentActivity, ...) — so a label
  // never gets shown as if it were output.
  const preview = stripAnsi(row.result?.text ?? (row.call.tool?.lines !== undefined ? row.call.text : ""));
  const full = stripAnsi(fullOutput(resultPayload));
  const shownLines = preview ? preview.split("\n").length : 0;
  const total = row.result?.tool?.lines ?? row.call.tool?.lines;
  const more = full && total !== undefined ? Math.max(0, total - shownLines) : 0;
  const showOutput = more === 0 && full.length > preview.length;
  const truncated = (row.result ?? row.call).truncated;
  const withMarker = (text: string) => (truncated && text ? `${text}\n(truncated)` : text);
  const shown = open ? full || preview : preview;
  return <div className="native-session-row" data-status={status} aria-label={`Native event ${seq}`} role="article">
    <p className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{g.bullet}</span><span>{toolHeader(provider, row.call)}</span>
      {status === "running" && <span className="native-session-muted"> {provider === "claude" ? "running…" : "…"}</span>}</p>
    {status === "error" && <p className="native-session-output native-session-error"><span aria-hidden="true">{g.output}</span> Error</p>}
    {isDiff && <pre className="native-session-output native-session-diff">
      {diff.map((line, i) => <span key={i} data-diff={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx"}>{line}{"\n"}</span>)}
      {truncated && <span>(truncated)</span>}
    </pre>}
    {!isDiff && shown && <pre className="native-session-output"><span aria-hidden="true">{g.output} </span>{withMarker(shown)}</pre>}
    {!isDiff && (more > 0 || showOutput) && <button type="button" className="native-session-more" aria-expanded={open} onClick={() => setOpen(o => !o)}>{open ? "collapse" : more > 0 ? `… +${more} lines` : "show output"}</button>}
  </div>;
}

function Working({ provider, since }: { provider: Provider; since?: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  const started = since ? Date.parse(since) : NaN;
  const seconds = Number.isFinite(started) ? Math.max(0, Math.round((now - started) / 1000)) : null;
  return <p className="native-session-line native-session-working" role="status" aria-label="Working">
    <span className="native-session-glyph" aria-hidden="true">{GLYPH[provider].working}</span>Working…{seconds !== null && <span className="native-session-muted"> {seconds}s</span>}</p>;
}

export function NativeSessionTranscript({ provider, events, busy, ownerName, participants }: {
  provider: Provider; events: readonly LocalSessionEvent[]; busy: boolean; ownerName: string; participants: readonly Person[];
}) {
  const rows = buildRows(events);
  const g = GLYPH[provider];
  const name = (id: string | null) => (id ? participants.find(p => p.id === id)?.displayName ?? "Teammate" : ownerName);
  const time = (ts?: string) => { const d = ts ? new Date(ts) : null; return d && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null; };
  const last = events.at(-1);
  const turnOpen = busy && last?.lifecycle !== "turn_end" && last?.lifecycle !== "turn_aborted";
  return <div className={`native-session native-session-${provider}`}>
    {rows.map(row => {
      if (row.kind === "prompt") return <div key={row.seq} className="native-session-row native-session-prompt" role="article" aria-label={`Native event ${row.seq}`}>
        <p className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{g.prompt}</span><span className="native-session-prompt-text">{row.text}</span></p>
        <p className="native-session-meta"><span>{name(row.who)}</span>{time(row.ts) && <span> · {time(row.ts)}</span>}</p>
      </div>;
      if (row.kind === "prose") return <div key={row.seq} className="native-session-row" role="article" aria-label={`Native event ${row.seq}`}>
        {provider === "codex" && row.final && <p className="native-session-codex-label">codex</p>}
        <div className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{provider === "codex" && row.final ? "" : g.bullet}</span>
          <div className="native-session-prose room-message-content"><MarkdownView skipHtml components={roomMarkdown}>{row.text}</MarkdownView></div></div>
      </div>;
      if (row.kind === "thinking") return <div key={row.seq} className="native-session-row native-session-thinking" role="article" aria-label={`Native event ${row.seq}`}>
        <div className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{g.thinking}</span>
          {row.text ? <div className="native-session-thought room-message-content"><MarkdownView skipHtml components={roomMarkdown}>{row.text}</MarkdownView></div> : <span>Thinking…</span>}</div>
      </div>;
      if (row.kind === "tool") return <ToolRow key={row.seq} provider={provider} row={row} seq={row.seq} />;
      const worked = row.lifecycle === "turn_end" ? formatMs(row.text) : "";
      const label = row.lifecycle === "turn_end" ? (worked ? `Worked for ${worked}` : "")
        : row.lifecycle === "turn_aborted" ? "Interrupted"
        : row.lifecycle === "compaction" ? "Context compacted"
        : row.lifecycle === "approval_pending" ? "Waiting for approval on the owner's Mac"
        : "Turn started";
      if (!label) return null;
      return <p key={row.seq} className="native-session-meta" role="article" aria-label={`Native event ${row.seq}`}>{label}</p>;
    })}
    {turnOpen && <Working provider={provider} since={last?.ts} />}
  </div>;
}

function formatMs(text: string): string {
  const ms = Number(text);
  if (!Number.isFinite(ms) || ms <= 0) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}
