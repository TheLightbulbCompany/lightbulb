"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  sessionGlyph, backgroundWaiting, buildRows, groupRows, lifecycleLabel, sessionClock, sessionSubagents, subagentLabel, subagentState, subagentStatus, subagentStrip, subagentTags,
  thinkingLabel, toolHeader, toolRowView,
  type DiffLine, type LocalSessionEvent, type LocalSessionProvider, type SessionReceiptChip, type SessionSubagent, type ThinkingTranscriptRow, type ToolGroupItem,
  type ToolTranscriptRow, type TranscriptRow,
} from "@lightbulb/shared";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "./roomMarkdown";

export type Provider = LocalSessionProvider;
export type Person = { kind: "human" | "agent"; id: string; displayName: string };
/** What a subagent was asked to do, by its id; names a hand-back's "Report from …" line. */
type SubagentName = (id: string) => string | undefined;

/** An edit's diff opens this far; a terminal prints the whole hunk, a pane has less room. */
const DIFF_LINES = 12;

/** A diff the way both terminals print it: the line number, the mark, the line. */
export function Diff({ lines, truncated }: { lines: readonly DiffLine[]; truncated: boolean }) {
  const width = String(lines.reduce((max, line) => Math.max(max, line.n ?? 0), 0)).length;
  return <pre className="native-session-output native-session-diff" style={{ ["--diff-digits" as string]: width }}>
    {lines.map((line, i) => <span key={i} className="native-session-diff-line" data-diff={line.mark === "+" ? "add" : line.mark === "-" ? "del" : "ctx"}>
      <span className="native-session-diff-n" aria-hidden="true">{line.n ?? ""}</span>
      <span className="native-session-diff-mark">{line.mark}</span>{line.text}{"\n"}
    </span>)}
    {truncated && <span>(truncated)</span>}
  </pre>;
}

function ToolRow({ provider, row, seq, subagentName }: { provider: Provider; row: ToolTranscriptRow; seq: number; subagentName?: SubagentName }) {
  const [open, setOpen] = useState(false);
  const g = sessionGlyph(provider);
  const { status, preview, full, numbered, summary, more, showOutput, truncated } = toolRowView(row);
  const isDiff = numbered.length > 0;
  const withMarker = (text: string) => (truncated && text ? `${text}\n(truncated)` : text);
  // Structured tool results are implementation details, often one enormous JSON line.
  // Keep the activity visible, with the payload available on demand.
  const structured = /^\s*[\[{]/.test(preview || full);
  const shown = open ? full || preview : structured ? "" : preview;
  const hidden = isDiff ? Math.max(0, numbered.length - DIFF_LINES) : more;
  const canExpand = hidden > 0 || (!isDiff && (showOutput || structured));
  if (row.call.tool?.name === "AgentReport") {
    // Claude Code's collapsed `› Message from @agent (expand)`.
    return <div className="native-session-row" aria-label={`Native event ${seq}`} role="article">
      <p className="native-session-line native-session-tool"><span className="native-session-glyph" aria-hidden="true">›</span><span>{toolHeader(provider, row.call, subagentName)}</span>
        <button type="button" className="native-session-more native-session-inline-more" aria-expanded={open} onClick={() => setOpen(o => !o)}>{open ? "(collapse)" : "(expand)"}</button></p>
      {open && <pre className="native-session-output">{withMarker(full)}</pre>}
    </div>;
  }
  return <div className="native-session-row" data-status={status} aria-label={`Native event ${seq}`} role="article">
    <p className="native-session-line native-session-tool"><span className="native-session-glyph" aria-hidden="true">{g.bullet}</span><span>{toolHeader(provider, row.call, subagentName)}</span>
      {status === "running" && <span className="native-session-muted"> {provider === "claude" ? "running…" : "…"}</span>}</p>
    {status === "error" && <p className="native-session-output native-session-error"><span aria-hidden="true">{g.output}</span> Error</p>}
    {summary && <p className="native-session-output"><span aria-hidden="true">{g.output} </span>{summary}</p>}
    {isDiff && <Diff lines={open ? numbered : numbered.slice(0, DIFF_LINES)} truncated={truncated} />}
    {!isDiff && shown && <pre className="native-session-output native-session-output-hang"><span aria-hidden="true">{g.output} </span>{withMarker(shown)}</pre>}
    {canExpand && <button type="button" className="native-session-more" aria-expanded={open} onClick={() => setOpen(o => !o)}>{open ? "collapse" : hidden > 0 ? `… +${hidden} lines` : "show output"}</button>}
  </div>;
}

/** Collapsed to one muted line; the reasoning itself is a click away. */
function ThinkingRow({ provider, row, live }: { provider: Provider; row: ThinkingTranscriptRow; live: boolean }) {
  const [open, setOpen] = useState(false);
  const label = thinkingLabel(row, live);
  return <div className="native-session-row native-session-thinking" role="article" aria-label={`Native event ${row.seq}`}>
    <div className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{sessionGlyph(provider).thinking}</span>
      {row.text ? <button type="button" className="native-session-toggle" aria-expanded={open} onClick={() => setOpen(o => !o)}>{label}<span className="native-session-chevron" aria-hidden="true">›</span></button> : <span>{label}</span>}</div>
    {open && row.text && <div className="native-session-thought room-message-content"><MarkdownView skipHtml components={roomMarkdown}>{row.text}</MarkdownView></div>}
  </div>;
}

/**
 * A folded run of tool calls, in its terminal's own words. Claude Code prints
 * one unbulleted line ("Read 2 files, ran 1 shell command"); Codex prints
 * "Explored" over one line per call. Either opens onto the calls themselves,
 * which is what ctrl+o does in the terminal.
 */
function ToolGroup({ provider, group, subagentName }: { provider: Provider; group: ToolGroupItem; subagentName?: SubagentName }) {
  const [open, setOpen] = useState(false);
  const g = sessionGlyph(provider);
  // A thought on its own has no calls behind it to open.
  if (!group.rows.some(row => row.kind === "tool")) return <div className="native-session-group" data-provider={provider}>
    <p className="native-session-line native-session-muted native-session-activity" role="article" aria-label={`Native event ${group.seq}`}>
      <span className="native-session-glyph" aria-hidden="true" />{group.summary}</p>
  </div>;
  return <div className="native-session-group" data-provider={provider}>
    <p className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{group.lines ? g.bullet : ""}</span>
      <button type="button" className="native-session-toggle native-session-activity" aria-expanded={open} onClick={() => setOpen(o => !o)}>{group.summary}<span className="native-session-chevron" aria-hidden="true">›</span></button></p>
    {!open && group.lines?.map((line, i) => <p key={i} className="native-session-output native-session-explored"><span aria-hidden="true">{i === 0 ? g.output : " "} </span>{line}</p>)}
    {open && <div className="native-session-group-rows">
      {group.rows.map(row => row.kind === "tool" && <ToolRow key={row.seq} provider={provider} row={row} seq={row.seq} subagentName={subagentName} />)}
    </div>}
  </div>;
}

/** Claude Code's spinner words; one per turn, the way its status line picks one. */
const SPINNER_VERBS = ["Thinking", "Pondering", "Cogitating", "Musing", "Brewing", "Computing", "Noodling", "Percolating"];

/** The live status line: Claude's `✻ Pondering… (2m 14s)`, Codex's `• Working (12s)`; the clock is the turn's, as in the terminal. */
function Working({ provider, since, seq }: { provider: Provider; since?: string; seq: number }) {
  const now = useNow(true);
  const started = since ? Date.parse(since) : NaN;
  const elapsed = Number.isFinite(started) ? sessionClock(Math.max(0, now - started), true) : null;
  const said = provider === "claude" ? `${SPINNER_VERBS[seq % SPINNER_VERBS.length]}…` : "Working";
  return <p className="native-session-line native-session-working" role="status" aria-label="Working" data-reload-blocker>
    <span className="native-session-glyph native-session-spinner" aria-hidden="true">{sessionGlyph(provider).working}</span>{said}{elapsed !== null && <span className="native-session-muted"> ({elapsed})</span>}</p>;
}

/** Claude Code's `✻ Waiting for N background agents to finish`, once its own turn is over. */
function Waiting({ subs, status }: { subs: readonly SessionSubagent[]; status?: string }) {
  const now = useNow(true);
  const n = backgroundWaiting(subs, status, now);
  if (!n) return null;
  return <p className="native-session-line native-session-working" role="status">
    <span className="native-session-glyph" aria-hidden="true">✻</span>Waiting for {n} background agent{n === 1 ? "" : "s"} to finish</p>;
}

/** The one status chip a follow-up carries; the longer explanation is its tooltip. */
export function ReceiptChip({ chip }: { chip: SessionReceiptChip }) {
  return <span role="status" className="native-session-chip" data-tone={chip.tone} title={chip.detail ?? undefined}>
    {chip.label}{chip.detail && <span className="sr-only">. {chip.detail}</span>}</span>;
}

/** Ticks once a second while `on`; a subagent's live clock. */
function useNow(on: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [on]);
  return now;
}

type TranscriptProps = {
  provider: Provider; events: readonly LocalSessionEvent[]; busy: boolean; ownerName: string; participants: readonly Person[];
  /** Whose rows to draw: a subagent's id, nested under its row; the session's own when absent. */
  scope?: string;
  /** The session's own status: a subagent with no end is "Ended" once its session is not working. */
  status?: string;
};

/** Asks the inline row of subagent `id` inside `root` to open, and brings it into view. */
export function revealSubagent(root: HTMLElement | null, id: string) {
  const row = root?.querySelector<HTMLElement>(`[data-subagent="${id}"]`);
  row?.dispatchEvent(new CustomEvent(OPEN_SUBAGENT));
  row?.scrollIntoView({ block: "center", behavior: "smooth" });
  row?.focus({ preventScroll: true });
}
const OPEN_SUBAGENT = "lightbulb:open-subagent";

/**
 * A subagent inline where it was launched, as the Claude Code terminal draws it:
 * `⏺ Agent · <description>`, then a `⎿` status line (live while it runs). It opens
 * in place onto its own transcript, nested under the row.
 */
function AgentRow({ subagent: s, seq, earlier, ...transcript }: TranscriptProps & { subagent: SessionSubagent; seq: number; earlier?: boolean }) {
  const [open, setOpen] = useState(false);
  const node = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const row = node.current;
    const reveal = () => setOpen(true);
    row?.addEventListener(OPEN_SUBAGENT, reveal);
    return () => row?.removeEventListener(OPEN_SUBAGENT, reveal);
  }, []);
  const now = useNow(s.state === "running");
  const state = subagentState(s, transcript.status, now);
  const { provider } = transcript;
  const g = sessionGlyph(provider);
  const name = s.description || "subagent";
  const tags = [...subagentTags(s), ...(earlier ? ["started earlier"] : [])];
  // Claude's `⏺ Agent · <what>`; Codex's own `• Spawned <nickname>` wording.
  const head = <>{provider === "codex" ? `Spawned ${name}` : `Agent · ${name}`}{tags.map(tag => <span key={tag} className="native-session-tag">{tag}</span>)}</>;
  return <div ref={node} className="native-session-row native-session-agent" role="article" aria-label={`Native event ${seq}`} data-subagent={s.id || undefined} data-state={state} tabIndex={-1}>
    <p className="native-session-line native-session-tool"><span className="native-session-glyph" aria-hidden="true">{g.bullet}</span>
      {s.id ? <button type="button" className="native-session-toggle native-session-agent-toggle" aria-expanded={open} onClick={() => setOpen(o => !o)}>{head}<span className="native-session-chevron" aria-hidden="true">›</span></button> : <span>{head}</span>}</p>
    <p className={`native-session-output native-session-agent-status${state === "failed" ? " native-session-error" : ""}`}><span aria-hidden="true">{g.output} </span>{subagentStatus(provider, s, now, transcript.status)}</p>
    {open && <div className="native-session-nested"><NativeSessionTranscript {...transcript} scope={s.id} busy={state === "running"} /></div>}
  </div>;
}

/**
 * Claude Code's panel above its prompt: `● main`, then one row per subagent still
 * running (`○ <type>  <what it is doing>`, its clock and tokens right-aligned), five
 * at most with `↓ N more`. A row opens its subagent inline. Codex has no such panel.
 */
export function SubagentPanel({ provider, events, status, onPick }: { provider: Provider; events: readonly LocalSessionEvent[]; status?: string; onPick: (id: string) => void }) {
  const subs = useMemo(() => sessionSubagents(events), [events]);
  const [all, setAll] = useState(false);
  const now = useNow(provider === "claude" && subs.some(s => s.state === "running"));
  if (provider !== "claude") return null;
  const strip = subagentStrip(provider, subs, status, now, all ? Infinity : 5);
  if (!strip.rows.length) return null;
  return <div className="native-session-panel" role="region" aria-label="Running subagents">
    <p className="native-session-panel-row"><span className="native-session-panel-dot" aria-hidden="true">●</span><span>main</span></p>
    {strip.rows.map(r => <button key={r.id} type="button" className="native-session-panel-row" onClick={() => onPick(r.id)}>
      <span aria-hidden="true">○</span><span className="native-session-panel-label">{r.label}</span><span className="native-session-panel-activity">{r.activity}</span>
      <span className="native-session-panel-meta">{sessionClock(r.elapsedMs, true)}{r.tokens != null ? ` · ↓ ${r.tokens.toLocaleString()} tokens` : ""}</span>
    </button>)}
    {strip.more > 0 && <button type="button" className="native-session-panel-row native-session-muted" onClick={() => setAll(true)}>↓ {strip.more} more</button>}
  </div>;
}

export function NativeSessionTranscript({ provider, events, busy, ownerName, participants, pending, outbox, scope, status }: TranscriptProps & {
  /** A follow-up sent from here that the harness has not echoed back yet: drawn greyed at the end, like Claude Code's queued prompt. */
  pending?: { text: string; chip: SessionReceiptChip | null; onCancel?: () => void; onRetry?: () => void } | null;
  outbox?: { requestId: string; text: string; chip: SessionReceiptChip | null; onCancel?: () => void; onRetry?: () => void }[];
}) {
  const rows = useMemo(() => buildRows(events, scope), [events, scope]);
  const subs = useMemo(() => sessionSubagents(events), [events]);
  const subagentName = useMemo(() => {
    const names = new Map(subs.map(s => [s.id, s.description]));
    return (id: string) => names.get(id) || undefined;
  }, [subs]);
  const root = useRef<HTMLDivElement>(null);
  // A background subagent's end points at its row, which sits in this same transcript (ids are opaque: no quoting needed).
  const showSubagent = (id: string) => revealSubagent(root.current, id);
  const g = sessionGlyph(provider);
  const name = (id: string | null) => (id ? participants.find(p => p.id === id)?.displayName ?? "Teammate" : ownerName);
  const time = (ts?: string) => { const d = ts ? new Date(ts) : null; return d && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null; };
  const last = events.findLast(e => e.subagent === scope);
  const turnOpen = busy && last?.lifecycle !== "turn_end" && last?.lifecycle !== "turn_aborted";
  // When the open turn began: its latest prompt, else its first event after the previous end.
  const turnStart = useMemo(() => {
    let first: string | undefined;
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i];
      if (e.subagent !== scope) continue;
      if (e.lifecycle === "turn_end" || e.lifecycle === "turn_aborted") break;
      // The latest prompt starts the turn; a late background report after the last end must not.
      if (e.kind === "text" && e.role === "user" && e.ts) return e.ts;
      if (e.ts) first = e.ts;
    }
    return first;
  }, [events, scope]);
  const lastRow = rows.at(-1);
  const pendingRow = (item: { text: string; chip: SessionReceiptChip | null; onCancel?: () => void; onRetry?: () => void }, key: string) =>
    <div key={key} className={`native-session-row native-session-prompt${item.chip?.tone === "error" ? "" : " native-session-pending"}`} role="article" aria-label={key === "current" ? "Pending follow-up" : "Follow-up delivery"}>
      <p className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{g.prompt}</span><span className="native-session-prompt-text">{item.text}</span></p>
      <p className="native-session-meta"><span>{ownerName}</span>{item.chip && <> · <ReceiptChip chip={item.chip} />{item.chip.tone === "error" && item.chip.detail && <span> · {item.chip.detail}</span>}</>}
        {item.onRetry && <> · <button type="button" onClick={item.onRetry} aria-label={`Retry follow-up: ${item.text.slice(0, 40)}`}>Retry</button></>}
        {item.onCancel && <> · <button type="button" onClick={item.onCancel} aria-label={`Cancel follow-up: ${item.text.slice(0, 40)}`}>Cancel</button></>}
      </p>
    </div>;
  const renderRow = (row: TranscriptRow) => {
    if (row.kind === "prompt") return <div key={row.seq} className="native-session-row native-session-prompt" role="article" aria-label={`Native event ${row.seq}`} data-seq={row.seq}>
      <p className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{g.prompt}</span><span className="native-session-prompt-text">{row.text}</span></p>
      <p className="native-session-meta"><span>{name(row.who)}</span>{time(row.ts) && <span> · {time(row.ts)}</span>}</p>
    </div>;
    if (row.kind === "prose") return <div key={row.seq} className="native-session-row" role="article" aria-label={`Native event ${row.seq}`} data-seq={row.seq}>
      <div className="native-session-line"><span className="native-session-glyph" aria-hidden="true">{g.bullet}</span>
        <div className="native-session-prose room-message-content"><MarkdownView skipHtml components={roomMarkdown}>{row.text}</MarkdownView></div></div>
    </div>;
    if (row.kind === "thinking") return <ThinkingRow key={row.seq} provider={provider} row={row} live={turnOpen && row === lastRow} />;
    if (row.kind === "tool") return <ToolRow key={row.seq} provider={provider} row={row} seq={row.seq} subagentName={subagentName} />;
    if (row.kind === "subagent") {
      const label = subagentLabel(row);
      // Claude Code's `● Agent "X" finished · 5h 20m`, its dot green (red when it failed).
      return <p key={row.seq} className="native-session-line native-session-finished" role="article" aria-label={`Native event ${row.seq}`} data-status={row.failed ? "error" : "done"}>
        <span className="native-session-glyph" aria-hidden="true">●</span>
        <button type="button" className="native-session-subagent" onClick={() => showSubagent(row.id)}>{label}</button>
      </p>;
    }
    if (row.kind === "agent") return <AgentRow key={row.seq} seq={row.seq} subagent={row.subagent} earlier={row.earlier} provider={provider} events={events} busy={busy} status={status} ownerName="Parent agent" participants={participants} />;
    if (row.kind === "notice") return <p key={row.seq} className="native-session-meta" role="article" aria-label={`Native event ${row.seq}`}>{row.text}</p>;
    const label = lifecycleLabel(row);
    if (!label) return null;
    // Claude Code closes a turn with `✻ Worked for 25s`; Codex prints no such line of its own.
    if (row.lifecycle === "turn_end" && provider === "claude") return <p key={row.seq} className="native-session-line native-session-muted native-session-turn-end" role="article" aria-label={`Native event ${row.seq}`}>
      <span className="native-session-glyph" aria-hidden="true">{g.working}</span>{label}</p>;
    return <p key={row.seq} className="native-session-meta" role="article" aria-label={`Native event ${row.seq}`}>{label}</p>;
  };
  return <div ref={root} className={`native-session native-session-${provider}`}>
    {groupRows(rows, turnOpen, provider).map(item => (item.kind === "tools" ? <ToolGroup key={item.seq} provider={provider} group={item} subagentName={subagentName} /> : renderRow(item)))}
    {turnOpen && <Working provider={provider} since={turnStart} seq={last?.seq ?? 0} />}
    {!turnOpen && !scope && provider === "claude" && <Waiting subs={subs} status={status} />}
    {outbox?.map(item => pendingRow(item, item.requestId))}
    {pending && pendingRow(pending, "current")}
  </div>;
}
