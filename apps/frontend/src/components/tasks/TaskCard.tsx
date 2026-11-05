"use client";

// A thread card under a Lightbulb message (mockup steps 2–9). The whole card opens the thread
// (a stretched button); the one action button sits above it. State colour marks the word and a
// dot, never a fill (DESIGN.md).

import { useState, type ReactElement } from "react";
import { specialistLabel, summaryView, taskSummaryLine, type TaskSummary, type TaskTone } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { capture } from "@/lib/analytics";
import { cn } from "@/lib/utils";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { ACTION_LABELS, useTaskActions } from "./useTaskActions";

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

export interface TaskCardProps {
  /** Undefined: the API no longer returns it (dismissed, hidden, deleted, or not yours to see). */
  task: TaskSummary | undefined;
  taskId: string;
  viewerId: string;
  onOpen?: (taskId: string) => void;
  /** Hide the action (the Suggested block renders its own Start/Dismiss). */
  hideAction?: boolean;
  children?: React.ReactNode;
}

export function TaskCard({ task, taskId, viewerId, onOpen, hideAction, children }: TaskCardProps): ReactElement {
  const { agentNames } = useLiveEvents();
  const { run } = useTaskActions("card");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!task) {
    return <div className="mt-1.5 max-w-xl rounded-lg border border-dashed border-border px-3 py-2 text-ui text-muted-foreground" data-task-card={taskId}>
      This thread isn&apos;t available
    </div>;
  }
  const view = summaryView(task, viewerId);
  // Free-text answers and plain reviews need the thread; everything else is one press.
  const inline = !hideAction && view.action && view.action !== "answer" && view.action !== "review" ? view.action : null;
  const open = () => {
    capture("task_card_opened", { taskId, source: "card", status: task.status });
    onOpen?.(taskId);
  };
  // The summary line only when it says more than the status word (an ask, a reason, what is landing).
  const line = taskSummaryLine(task, viewerId, agentNames);
  const detail = line === view.word || line.startsWith(`${view.word} ·`) ? null : line;
  return <div className="editorial-card editorial-card-hover relative mt-1.5 max-w-xl px-3 py-2" data-task-card={taskId}>
    <div className="flex items-start gap-3">
      <div className="min-w-0 flex-1">
        <button type="button" onClick={open} aria-label={`Open ${task.title}, ${view.word}`}
          className="block max-w-full truncate text-left font-semibold after:absolute after:inset-0 after:rounded-lg after:content-[''] focus-visible:outline-none focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-ring">
          {task.title}
        </button>
        <p className="truncate text-ui text-muted-foreground">{specialistLabel(task.specialist, agentNames)}</p>
        {detail && <p className="truncate text-ui">{detail}</p>}
      </div>
      <TaskStatus task={task} viewerId={viewerId} />
    </div>
    {task.output && !detail?.includes(task.output.title) && <p className="mt-1 truncate text-ui text-muted-foreground"><span aria-hidden="true">📎 </span>{task.output.title}</p>}
    {children}
    {inline && <div className="relative z-10 mt-2 flex items-center gap-2">
      <Button type="button" size="sm" variant={view.tone === "waiting" || view.tone === "review" ? "default" : "outline"} disabled={pending}
        onClick={async (event) => {
          event.stopPropagation();
          setPending(true);
          setError(null);
          const res = await run(task, inline);
          setPending(false);
          if (!res.ok) setError(res.message);
        }}>{ACTION_LABELS[inline]}</Button>
    </div>}
    {error && <p role="alert" className="relative z-10 mt-1 text-ui text-err">{error}</p>}
  </div>;
}
