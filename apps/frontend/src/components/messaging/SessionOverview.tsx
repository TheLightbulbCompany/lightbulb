"use client";

import { useMemo, type ReactNode } from "react";
import { sessionChanges, sessionPlan, type LocalSessionEvent, type PlanStatus } from "@lightbulb/shared";

const MARK: Record<PlanStatus, string> = { pending: "◻", in_progress: "◼", completed: "✔" };
const SAID: Record<PlanStatus, string> = { pending: "To do", in_progress: "In progress", completed: "Done" };

/**
 * The session at a glance, under Details → Changes: the live plan (Claude's
 * task list, Codex's update_plan) and what it changed. Both come from the
 * loaded history, so "Load older" can add earlier edits.
 */
export function SessionOverview({ events, changesAction, expanded = false }: {
  events: readonly LocalSessionEvent[];
  /** What the reader can do with the changes (e.g. "Commit & open PR"); shown inside "What changed". */
  changesAction?: ReactNode;
  /** Both sections start open: the reader came here for them (Details → Changes). */
  expanded?: boolean;
}) {
  const plan = useMemo(() => sessionPlan(events), [events]);
  const changes = useMemo(() => sessionChanges(events), [events]);
  if (!plan?.length && !changes.length) return null;
  const done = plan?.filter(step => step.status === "completed").length ?? 0;
  const added = changes.reduce((n, f) => n + f.added, 0);
  const removed = changes.reduce((n, f) => n + f.removed, 0);
  return <div className="local-session-overview">
    {plan && plan.length > 0 && <details open={expanded || done < plan.length}>
      <summary className="native-session-toggle">Plan · {done} of {plan.length} done</summary>
      <ul className="local-session-plan">
        {plan.map((step, i) => <li key={i} data-status={step.status}>
          <span aria-hidden="true">{MARK[step.status]}</span><span className="sr-only">{SAID[step.status]}: </span>{step.text}
        </li>)}
      </ul>
    </details>}
    {changes.length > 0 && <details open={expanded}>
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
  </div>;
}

function FileLine({ path, added, removed }: { path: string; added: number; removed: number }) {
  return <><span className="room-wrap">{path}</span><span className="local-session-add">+{added}</span><span className="local-session-del">−{removed}</span></>;
}
