"use client";

// A request card under a Lightbulb message (mockup steps 2–9). The whole card opens the request
// (a stretched button); the one action button sits above it. State colour marks the word and a
// dot, never a fill (DESIGN.md).

import { useEffect, useState, type ReactElement } from "react";
import { activityLine, specialistLabel, summaryView, taskSummaryLine, useTaskQuestions, type TaskSummary, type TaskTone } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { Button } from "@/components/ui/button";
import { capture } from "@/lib/analytics";
import { cn } from "@/lib/utils";
import { RequestApp } from "@/components/canvas/RequestApp";
import { TaskOutputs } from "@/components/creations/TaskOutputs";
import { DecisionCard } from "./DecisionCard";
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

/** The status as a dot alone, named for screen readers (a chip's live state). */
export function TaskDot({ task, viewerId }: { task: TaskSummary; viewerId: string }): ReactElement {
  const view = summaryView(task, viewerId);
  return <span role="img" aria-label={view.word} data-tone={view.tone}
    className={cn("size-1.5 shrink-0 rounded-full bg-current", TONE_TEXT[view.tone], view.tone === "working" && "motion-safe:animate-pulse")} />;
}

/** "Growth · working · 12s" while a request is Working (the timer ticks each second); the specialist alone otherwise.
 *  The actor is its own span, so it reads as the one doing the work. */
export function ActorLine({ task, viewerId, className, inline = false }: { task: TaskSummary; viewerId: string; className?: string; inline?: boolean }): ReactElement {
  const { agentNames } = useLiveEvents();
  const [now, setNow] = useState(() => Date.now());
  const working = summaryView(task, viewerId).word === "Working";
  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [working]);
  const live = activityLine(task, viewerId, agentNames, now);
  const Tag = inline ? "span" : "p";
  return <Tag className={cn("truncate text-ui text-muted-foreground", className)} data-live={live ? "" : undefined}>
    <span className={inline ? undefined : "text-foreground"}>{live?.actor ?? specialistLabel(task.specialist, agentNames)}</span>
    {live && <> · {live.rest}</>}
  </Tag>;
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
  // A question this viewer can answer is the one decision card, compact, on the card itself (UX audit slice 2):
  // Deny / Allow once / Always allow here too, never only the yes.
  const { tasks } = useLiveEvents();
  const q = task?.openQuestion;
  const asks = q?.canAnswer ? q.qid : null;
  const { data: views, error: viewsError } = useTaskQuestions(tasks, asks ? taskId : null);
  const question = views?.items.find((v) => v.id === asks && v.state === "open");
  // Details didn't load: the card says so and opens the task (where the question shows in full), never acting blind.
  const blind = !!asks && !views && !!viewsError;
  if (!task) {
    return <div className="mt-1.5 max-w-xl rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground" data-task-card={taskId}>
      This task isn&apos;t available
    </div>;
  }
  const view = summaryView(task, viewerId);
  // Free-text answers and plain reviews need the request pane; everything else is one press.
  // Reopen lives in the pane and the overview, not on every resolved card in the history (audit #30).
  const inline = !asks && view.action && view.action !== "answer" && view.action !== "review" && view.action !== "reopen" ? view.action : null;
  const open = () => {
    capture("task_card_opened", { taskId, source: "card", status: task.status });
    onOpen?.(taskId);
  };
  // The summary line only when it says more than the status word (an ask, a reason, what is landing).
  // The specialist is the actor, by its own name ("Growth"), on its own line (founder, 5 Nov).
  const line = taskSummaryLine(task, viewerId);
  // With the decision card open below, its question says it: no "Needs approval: …" line above it.
  const detail = line === view.word || question ? null : line;
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
    {/* One chip per thing it made; an app's chip replaces the old app line. */}
    <TaskOutputs task={task} max={2} onOpenTask={open} />
    {!task.outputs?.some((o) => o.kind === "app") && <RequestApp taskId={taskId} />}
    {question && <div className="relative z-10 mt-2"><DecisionCard task={task} question={question} compact framed={false} /></div>}
    {inline && <div className="relative z-10 mt-2 flex items-center gap-2">
      <Button type="button" size="sm" variant={view.tone === "waiting" || view.tone === "review" ? "default" : "outline"} disabled={pending}
        onClick={async (event) => {
          event.stopPropagation();
          setPending(true);
          setError(null);
          const res = await run(task, inline);
          setPending(false);
          if (!res.ok) setError(res.message);
        }}>{actionLabel(task, inline)}</Button>
    </div>}
    {blind && <p className="relative z-10 mt-1 text-ui text-muted-foreground">The details didn&apos;t load. Open the task to decide.</p>}
    {error && <p role="alert" className="relative z-10 mt-1 text-ui text-err">{error}</p>}
  </div>;
}
