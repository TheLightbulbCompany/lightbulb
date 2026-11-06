"use client";

// A request card under a Lightbulb message (mockup steps 2–9). The whole card opens the request
// (a stretched button); the one action button sits above it. State colour marks the word and a
// dot, never a fill (DESIGN.md).

import { useEffect, useState, type ReactElement } from "react";
import { activityLine, previewRows, specialistLabel, summaryView, taskSummaryLine, useTaskQuestions, type TaskSummary, type TaskTone } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { Button } from "@/components/ui/button";
import { capture } from "@/lib/analytics";
import { cn } from "@/lib/utils";
import { RequestApp } from "@/components/canvas/RequestApp";
import { actionLabel, useTaskActions } from "./useTaskActions";

const TONE_TEXT: Record<TaskTone, string> = {
  working: "text-info", waiting: "text-warn", review: "text-foreground", landing: "text-ok", neutral: "text-muted-foreground",
};

export function TaskStatus({ task, viewerId }: { task: TaskSummary; viewerId: string }): ReactElement {
  const view = summaryView(task, viewerId);
  return <span className={cn("inline-flex shrink-0 items-center gap-1.5 text-caption font-semibold", TONE_TEXT[view.tone])} data-tone={view.tone}>
    <span aria-hidden="true" className={cn("size-1.5 rounded-full bg-current", view.tone === "working" && "motion-safe:animate-pulse")} />
    {view.word}
  </span>;
}

/** "Growth · working · 12s" while a request is Working (the timer ticks each second); the specialist alone otherwise.
 *  The actor is its own span, so it reads as the one doing the work. */
export function ActorLine({ task, viewerId, className }: { task: TaskSummary; viewerId: string; className?: string }): ReactElement {
  const { agentNames } = useLiveEvents();
  const [now, setNow] = useState(() => Date.now());
  const working = summaryView(task, viewerId).word === "Working";
  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [working]);
  const live = activityLine(task, viewerId, agentNames, now);
  return <p className={cn("truncate text-ui text-muted-foreground", className)} data-live={live ? "" : undefined}>
    <span className="text-foreground">{live?.actor ?? specialistLabel(task.specialist, agentNames)}</span>
    {live && <> · {live.rest}</>}
  </p>;
}

export interface TaskCardProps {
  /** Undefined: the API no longer returns it (dismissed, hidden, deleted, or not yours to see). */
  task: TaskSummary | undefined;
  taskId: string;
  viewerId: string;
  onOpen?: (taskId: string) => void;
}

export function TaskCard({ task, taskId, viewerId, onOpen }: TaskCardProps): ReactElement {
  const { run } = useTaskActions("card");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // An approval or review this viewer can act on shows what it does before the one press (To, Subject, …).
  const { tasks } = useLiveEvents();
  const q = task?.openQuestion;
  const asks = q?.canAnswer && (q.kind === "approval" || q.kind === "review") ? q.qid : null;
  const { data: views, error: viewsError } = useTaskQuestions(tasks, asks ? taskId : null);
  // Details didn't load: the button opens the request (where the question shows in full) instead of acting blind.
  const blind = !!asks && !views && !!viewsError;
  const preview = previewRows(views?.items.find((q) => q.id === asks)?.preview).slice(0, 3);
  if (!task) {
    return <div className="mt-1.5 max-w-xl rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground" data-task-card={taskId}>
      This request isn&apos;t available
    </div>;
  }
  const view = summaryView(task, viewerId);
  // Free-text answers and plain reviews need the request pane; everything else is one press.
  // Reopen lives in the pane and the overview, not on every resolved card in the history (audit #30).
  const inline = view.action && view.action !== "answer" && view.action !== "review" && view.action !== "reopen" ? view.action : null;
  const open = () => {
    capture("task_card_opened", { taskId, source: "card", status: task.status });
    onOpen?.(taskId);
  };
  // The summary line only when it says more than the status word (an ask, a reason, what is landing).
  // The specialist is the actor, by its own name ("Growth"), on its own line (founder, 5 Nov).
  const line = taskSummaryLine(task, viewerId);
  const detail = line === view.word ? null : line;
  return <div className="editorial-card editorial-card-hover relative mt-1.5 max-w-xl px-3 py-2" data-task-card={taskId}>
    <div className="flex items-start gap-3">
      <div className="min-w-0 flex-1">
        <button type="button" onClick={open} aria-label={`Open ${task.title}, ${view.word}`}
          className="block max-w-full truncate text-left font-semibold after:absolute after:inset-0 after:rounded-lg after:content-[''] focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-ring">
          {task.title}
        </button>
        <ActorLine task={task} viewerId={viewerId} />
        {detail && <p className="truncate text-ui">{detail}</p>}
      </div>
      <TaskStatus task={task} viewerId={viewerId} />
    </div>
    {preview.length > 0 && <dl aria-label="What this does" className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 text-ui">
      {preview.map((r) => <div key={r.label} className="contents">
        <dt className="text-muted-foreground">{r.label}</dt><dd className="min-w-0 truncate">{r.value}</dd>
      </div>)}
    </dl>}
    {task.output && !detail?.includes(task.output.title) && <p className="mt-1 truncate text-ui text-muted-foreground"><span aria-hidden="true">📎 </span>{task.output.title}</p>}
    <RequestApp taskId={taskId} />
    {inline && <div className="relative z-10 mt-2 flex items-center gap-2">
      <Button type="button" size="sm" variant={view.tone === "waiting" || view.tone === "review" ? "default" : "outline"} disabled={pending || (!!asks && !views && !blind)}
        onClick={async (event) => {
          event.stopPropagation();
          if (blind) { open(); return; }
          setPending(true);
          setError(null);
          const res = await run(task, inline);
          setPending(false);
          if (!res.ok) setError(res.message);
        }}>{actionLabel(task, inline)}</Button>
    </div>}
    {blind && <p className="relative z-10 mt-1 text-ui text-muted-foreground">The details didn&apos;t load. Open the request to decide.</p>}
    {error && <p role="alert" className="relative z-10 mt-1 text-ui text-err">{error}</p>}
  </div>;
}
