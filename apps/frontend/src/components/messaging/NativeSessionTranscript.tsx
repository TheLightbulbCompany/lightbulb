"use client";

import { useEffect, useState } from "react";
import {
  GLYPH, buildRows, lifecycleLabel, toolHeader, toolRowView,
  type LocalSessionEvent, type LocalSessionProvider, type ToolTranscriptRow,
} from "@lightbulb/shared";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "./roomMarkdown";

export type Provider = LocalSessionProvider;
export type Person = { kind: "human" | "agent"; id: string; displayName: string };

function ToolRow({ provider, row, seq }: { provider: Provider; row: ToolTranscriptRow; seq: number }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const { status, preview, full, diff, more, showOutput, truncated } = toolRowView(row);
  const isDiff = diff.length > 0;
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
      if (row.kind === "notice") return <p key={row.seq} className="native-session-meta" role="article" aria-label={`Native event ${row.seq}`}>{row.text}</p>;
      const label = lifecycleLabel(row);
      if (!label) return null;
      return <p key={row.seq} className="native-session-meta" role="article" aria-label={`Native event ${row.seq}`}>{label}</p>;
    })}
    {turnOpen && <Working provider={provider} since={last?.ts} />}
  </div>;
}
