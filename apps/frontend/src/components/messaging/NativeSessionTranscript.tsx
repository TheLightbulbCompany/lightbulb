"use client";

import { useEffect, useState } from "react";
import {
  GLYPH, PREVIEW_LINES, buildRows, groupRows, lifecycleLabel, subagentLabel, thinkingLabel, toolHeader, toolRowView,
  type LocalSessionEvent, type LocalSessionProvider, type SessionReceiptChip, type ThinkingTranscriptRow, type ToolGroupItem,
  type ToolTranscriptRow, type TranscriptRow,
} from "@lightbulb/shared";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "./roomMarkdown";

export type Provider = LocalSessionProvider;
export type Person = { kind: "human" | "agent"; id: string; displayName: string };
/** What a subagent was asked to do, by its id; names a hand-back's "Report from …" line. */
type SubagentName = (id: string) => string | undefined;

function ToolRow({ provider, row, seq, subagentName }: { provider: Provider; row: ToolTranscriptRow; seq: number; subagentName?: SubagentName }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const { status, preview, full, diff, more, showOutput, truncated } = toolRowView(row);
  const isDiff = diff.length > 0;
  const withMarker = (text: string) => (truncated && text ? `${text}\n(truncated)` : text);
  const shown = open ? full || preview : preview;
  const hidden = isDiff ? Math.max(0, diff.length - PREVIEW_LINES) : more;
  const canExpand = hidden > 0 || (!isDiff && showOutput);
  return <div className="native-session-row" data-status={status} aria-label={`Native event ${seq}`} role="article">
    <p className="native-session-line native-session-tool"><span className="native-session-glyph" aria-hidden="true">{g.bullet}</span><span>{toolHeader(provider, row.call, subagentName)}</span>
      {status === "running" && <span className="native-session-muted"> {provider === "claude" ? "running…" : "…"}</span>}</p>
    {status === "error" && <p className="native-session-output native-session-error"><span aria-hidden="true">{g.output}</span> Error</p>}
    {isDiff && <pre className="native-session-output native-session-diff">
      {(open ? diff : diff.slice(0, PREVIEW_LINES)).map((line, i) => <span key={i} data-diff={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx"}>{line}{"\n"}</span>)}
      {truncated && <span>(truncated)</span>}
    </pre>}
    {!isDiff && shown && <pre className="native-session-output"><span aria-hidden="true">{g.output} </span>{withMarker(shown)}</pre>}
    {canExpand && <button type="button" className="native-session-more" aria-expanded={open} onClick={() => setOpen(o => !o)}>{open ? "collapse" : hidden > 0 ? `… +${hidden} lines` : "show output"}</button>}
  </div>;
}

/** Collapsed to one muted line; the reasoning itself is a click away. */
function ThinkingRow({ provider, row, live }: { provider: Provider; row: ThinkingTranscriptRow; live: boolean }) {
  const [open, setOpen] = useState(false);
  const label = thinkingLabel(row, live);
  return <div className="native-session-row native-session-thinking" role="article" aria-label={`Native event ${row.seq}`}>
    <div className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{GLYPH[provider].thinking}</span>
      {row.text ? <button type="button" className="native-session-toggle" aria-expanded={open} onClick={() => setOpen(o => !o)}>{label}<span className="native-session-chevron" aria-hidden="true">›</span></button> : <span>{label}</span>}</div>
    {open && row.text && <div className="native-session-thought room-message-content"><MarkdownView skipHtml components={roomMarkdown}>{row.text}</MarkdownView></div>}
  </div>;
}

/** A folded run of tool calls: one summary line that opens onto the individual rows. */
function ToolGroup({ provider, group, subagentName }: { provider: Provider; group: ToolGroupItem; subagentName?: SubagentName }) {
  const [open, setOpen] = useState(false);
  return <div className="native-session-group">
    <p className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{GLYPH[provider].bullet}</span>
      <button type="button" className="native-session-toggle" aria-expanded={open} onClick={() => setOpen(o => !o)}>{group.summary}<span className="native-session-chevron" aria-hidden="true">›</span></button></p>
    {open && <div className="native-session-group-rows">
      {group.rows.map(row => row.kind === "tool"
        ? <ToolRow key={row.seq} provider={provider} row={row} seq={row.seq} subagentName={subagentName} />
        : <ThinkingRow key={row.seq} provider={provider} row={row} live={false} />)}
    </div>}
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

/** The one status chip a follow-up carries; the longer explanation is its tooltip. */
export function ReceiptChip({ chip }: { chip: SessionReceiptChip }) {
  return <span role="status" className="native-session-chip" data-tone={chip.tone} title={chip.detail ?? undefined}>
    {chip.label}{chip.detail && <span className="sr-only">. {chip.detail}</span>}</span>;
}

export function NativeSessionTranscript({ provider, events, busy, ownerName, participants, pending, subagentName, onOpenSubagent }: {
  provider: Provider; events: readonly LocalSessionEvent[]; busy: boolean; ownerName: string; participants: readonly Person[];
  subagentName?: SubagentName;
  /** Opens a subagent's own transcript from the line its end drew here. */
  onOpenSubagent?: (id: string) => void;
  /** A follow-up sent from here that the harness has not echoed back yet: drawn greyed at the end, like Claude Code's queued prompt. */
  pending?: { text: string; chip: SessionReceiptChip | null } | null;
}) {
  const rows = buildRows(events);
  const g = GLYPH[provider];
  const name = (id: string | null) => (id ? participants.find(p => p.id === id)?.displayName ?? "Teammate" : ownerName);
  const time = (ts?: string) => { const d = ts ? new Date(ts) : null; return d && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null; };
  const last = events.at(-1);
  const turnOpen = busy && last?.lifecycle !== "turn_end" && last?.lifecycle !== "turn_aborted";
  const lastRow = rows.at(-1);
  const renderRow = (row: TranscriptRow) => {
    if (row.kind === "prompt") return <div key={row.seq} className="native-session-row native-session-prompt" role="article" aria-label={`Native event ${row.seq}`}>
      <p className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{g.prompt}</span><span className="native-session-prompt-text">{row.text}</span></p>
      <p className="native-session-meta"><span>{name(row.who)}</span>{time(row.ts) && <span> · {time(row.ts)}</span>}</p>
    </div>;
    if (row.kind === "prose") return <div key={row.seq} className="native-session-row" role="article" aria-label={`Native event ${row.seq}`}>
      {provider === "codex" && row.final && <p className="native-session-codex-label">codex</p>}
      <div className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{provider === "codex" && row.final ? "" : g.bullet}</span>
        <div className="native-session-prose room-message-content"><MarkdownView skipHtml components={roomMarkdown}>{row.text}</MarkdownView></div></div>
    </div>;
    if (row.kind === "thinking") return <ThinkingRow key={row.seq} provider={provider} row={row} live={turnOpen && row === lastRow} />;
    if (row.kind === "tool") return <ToolRow key={row.seq} provider={provider} row={row} seq={row.seq} subagentName={subagentName} />;
    if (row.kind === "subagent") {
      const label = subagentLabel(row);
      return <p key={row.seq} className="native-session-line native-session-muted" role="article" aria-label={`Native event ${row.seq}`} data-status={row.failed ? "error" : "done"}>
        <span className="native-session-glyph" aria-hidden="true">{g.output}</span>
        {onOpenSubagent ? <button type="button" className="native-session-subagent" onClick={() => onOpenSubagent(row.id)}>{label}</button> : <span>{label}</span>}
      </p>;
    }
    if (row.kind === "notice") return <p key={row.seq} className="native-session-meta" role="article" aria-label={`Native event ${row.seq}`}>{row.text}</p>;
    const label = lifecycleLabel(row);
    if (!label) return null;
    return <p key={row.seq} className="native-session-meta" role="article" aria-label={`Native event ${row.seq}`}>{label}</p>;
  };
  return <div className={`native-session native-session-${provider}`}>
    {groupRows(rows, turnOpen).map(item => (item.kind === "tools" ? <ToolGroup key={item.seq} provider={provider} group={item} subagentName={subagentName} /> : renderRow(item)))}
    {turnOpen && <Working provider={provider} since={last?.ts} />}
    {pending && <div className="native-session-row native-session-prompt native-session-pending" role="article" aria-label="Pending follow-up">
      <p className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{g.prompt}</span><span className="native-session-prompt-text">{pending.text}</span></p>
      <p className="native-session-meta"><span>{ownerName}</span>{pending.chip && <> · <ReceiptChip chip={pending.chip} /></>}</p>
    </div>}
  </div>;
}
