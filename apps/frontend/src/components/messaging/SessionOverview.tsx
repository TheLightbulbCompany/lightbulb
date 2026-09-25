"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { sessionChanges, sessionPlan, subagentElapsedMs, type LocalSessionEvent, type PlanStatus, type SessionSubagent } from "@lightbulb/shared";

const MARK: Record<PlanStatus, string> = { pending: "◻", in_progress: "◼", completed: "✔" };
const SAID: Record<PlanStatus, string> = { pending: "To do", in_progress: "In progress", completed: "Done" };

/**
 * The session at a glance, pinned above its transcript: the live plan (Claude's
 * task list, Codex's update_plan) and what it changed. Both come from the
 * loaded history, so "Load older" can add earlier edits.
 */
export function SessionOverview({ events, changesAction, subagents = [], openSubagent = null, onOpenSubagent }: {
  events: readonly LocalSessionEvent[];
  /** What the reader can do with the changes (e.g. "Commit & open PR"); shown inside "What changed". */
  changesAction?: ReactNode;
  /** The session's subagents; each opens its own transcript below. */
  subagents?: readonly SessionSubagent[];
  openSubagent?: string | null;
  onOpenSubagent?: (id: string | null) => void;
}) {
  const plan = useMemo(() => sessionPlan(events), [events]);
  const changes = useMemo(() => sessionChanges(events), [events]);
  if (!plan?.length && !changes.length && !subagents.length) return null;
  const done = plan?.filter(step => step.status === "completed").length ?? 0;
  const added = changes.reduce((n, f) => n + f.added, 0);
  const removed = changes.reduce((n, f) => n + f.removed, 0);
  return <div className="local-session-overview">
    {plan && plan.length > 0 && <details open={done < plan.length}>
      <summary className="native-session-toggle">Plan · {done} of {plan.length} done</summary>
      <ul className="local-session-plan">
        {plan.map((step, i) => <li key={i} data-status={step.status}>
          <span aria-hidden="true">{MARK[step.status]}</span><span className="sr-only">{SAID[step.status]}: </span>{step.text}
        </li>)}
      </ul>
    </details>}
    {changes.length > 0 && <details>
      <summary className="native-session-toggle">
        What changed · {changes.length} {changes.length === 1 ? "file" : "files"}
        <span className="local-session-add">+{added}</span><span className="local-session-del">−{removed}</span>
      </summary>
      {changesAction && <div className="my-1 flex flex-wrap items-center gap-2">{changesAction}</div>}
      <ul className="local-session-changes">
        {changes.map(file => <li key={file.path}>
          {file.diff.length > 0 ? <details>
            <summary className="native-session-toggle"><FileLine path={file.path} added={file.added} removed={file.removed} /></summary>
            <pre className="native-session-output native-session-diff">
              {file.diff.map((line, i) => <span key={i} data-diff={line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx"}>{line}{"\n"}</span>)}
            </pre>
          </details> : <p className="native-session-toggle"><FileLine path={file.path} added={file.added} removed={file.removed} /></p>}
        </li>)}
      </ul>
    </details>}
    {subagents.length > 0 && onOpenSubagent && <Subagents subagents={subagents} open={openSubagent} onOpen={onOpenSubagent} />}
  </div>;
}

const STATE: Record<SessionSubagent["state"], string> = { running: "Running", completed: "Done", failed: "Failed" };

function elapsed(ms: number | null) {
  if (ms == null) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor(s / 60) % 60}m`;
}

/** Like the Claude Code terminal's agent list: what each was asked, its state and clock; one opens its transcript. */
function Subagents({ subagents, open, onOpen }: { subagents: readonly SessionSubagent[]; open: string | null; onOpen: (id: string | null) => void }) {
  const running = subagents.filter(s => s.state === "running").length;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  return <details open={running > 0 || open !== null}>
    <summary className="native-session-toggle">Subagents · {running ? `${running} running` : `${subagents.length} done`}</summary>
    <ul className="local-session-subagents">
      {subagents.map(s => <li key={s.id} data-state={s.state}>
        <button type="button" className="native-session-toggle" aria-pressed={open === s.id} onClick={() => onOpen(open === s.id ? null : s.id)}>
          <span className={`native-session-dot native-session-dot-${s.state === "running" ? "busy" : s.state === "failed" ? "waiting-approval" : "idle"}`} aria-hidden="true" />
          <span className="room-wrap">{s.description || s.id}</span>
        </button>
        <span className="native-session-muted">
          {[s.type, STATE[s.state], s.background === true ? "background" : s.background === false ? "foreground" : null, elapsed(subagentElapsedMs(s, now))].filter(Boolean).join(" · ")}
        </span>
      </li>)}
    </ul>
  </details>;
}

function FileLine({ path, added, removed }: { path: string; added: number; removed: number }) {
  return <><span className="room-wrap">{path}</span><span className="local-session-add">+{added}</span><span className="local-session-del">−{removed}</span></>;
}
