"use client";

// Request cards under a Lightbulb message (`taskRefs`) and the "Sent to …" line under a person's message (`routedTo`). Only rendered with the switch on.
// One shared room list read (SWR dedupes it across every message in the room).

import type { ReactElement } from "react";
import { ChevronRight } from "lucide-react";
import { useTask, useTaskList, type CanonicalRoomMessage, type TaskSummary } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { TaskCard, TaskDot } from "./TaskCard";

export function MessageTasks({ message, viewerId, onOpen }: {
  message: CanonicalRoomMessage; viewerId: string; onOpen?: (taskId: string) => void;
}): ReactElement | null {
  const { tasks } = useLiveEvents();
  const refs = message.taskRefs ?? [];
  const { data, error, mutate } = useTaskList(tasks, refs.length ? { kind: "room", roomId: message.roomId } : null);
  if (!refs.length) return null;
  const byId = new Map((data?.tasks ?? []).map((t) => [t.taskId, t]));
  // Before the list loads, quiet placeholders (never a row of "isn't available"); a failed read offers Retry.
  if (!data) {
    return error
      ? <p role="alert" className="mt-1.5 max-w-xl text-ui text-muted-foreground">
        Tasks couldn&apos;t load. <button type="button" className="underline hover:text-foreground" onClick={() => void mutate()}>Retry</button>
      </p>
      : <div aria-busy="true" aria-label="Loading tasks">
        {refs.map((r) => <div key={r.taskId} className="mt-1.5 h-14 max-w-xl rounded-lg border border-border bg-muted/40 motion-safe:animate-pulse" />)}
      </div>;
  }
  // The Router's one-line result ("Done: …") carries its task as a chip, not a second card (UX slice 3).
  const Ref = message.kind === "task_line" ? RefChip : RefCard;
  return <div aria-label="Tasks in this message">
    {refs.map((r) => <Ref key={r.taskId} taskId={r.taskId} listed={byId.get(r.taskId)} viewerId={viewerId} onOpen={onOpen} />)}
  </div>;
}

/** One line: the task's live dot and title; it opens the task. */
function RefChip({ taskId, listed, viewerId, onOpen }: {
  taskId: string; listed: TaskSummary | undefined; viewerId: string; onOpen?: (taskId: string) => void;
}): ReactElement | null {
  const { tasks } = useLiveEvents();
  const { data } = useTask(tasks, listed ? null : taskId);
  const task = listed ?? data?.task;
  if (!task) return null;
  return <button type="button" data-task-chip={taskId} onClick={() => onOpen?.(taskId)}
    className="mt-1 inline-flex max-w-full items-center gap-1.5 rounded-md border border-border px-2 py-0.5 text-ui hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring">
    <TaskDot task={task} viewerId={viewerId} />
    <span className="min-w-0 truncate font-semibold">{task.title}</span>
    <ChevronRight aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
  </button>;
}

/** A card whose task the room list doesn't hold (older than its pages, or another scope) reads it by id. */
function RefCard({ taskId, listed, viewerId, onOpen }: {
  taskId: string; listed: TaskSummary | undefined; viewerId: string; onOpen?: (taskId: string) => void;
}): ReactElement | null {
  const { tasks } = useLiveEvents();
  const { data, error } = useTask(tasks, listed ? null : taskId);
  if (!listed && !data && !error) return null;
  return <TaskCard task={listed ?? data?.task} taskId={taskId} viewerId={viewerId} onOpen={onOpen} />;
}

/** "Sent to Workshop signup app ›" (one request, opens it) or "2 requests started ›" (scrolls to the reply). */
export function RoutedToLink({ routedTo, taskTitle, onOpen, onShowReply }: {
  routedTo: NonNullable<CanonicalRoomMessage["routedTo"]>;
  taskTitle?: (taskId: string) => string | undefined;
  onOpen?: (taskId: string) => void;
  onShowReply?: (taskId: string) => void;
}): ReactElement | null {
  const { taskIds, started, passThrough } = routedTo;
  if (!taskIds.length) return null;
  const single = passThrough || taskIds.length === 1;
  const label = single
    ? `Sent to ${taskTitle?.(taskIds[0]) ?? "a task"}`
    : `${started} ${started === 1 ? "task" : "tasks"} started`;
  return <button type="button" className="mt-0.5 text-ui text-muted-foreground hover:text-foreground hover:underline"
    onClick={() => (single ? onOpen?.(taskIds[0]) : onShowReply?.(taskIds[0]))}>{label} ›</button>;
}

/** `RoutedToLink` for a person's message, with the request's title from the room's list. */
export function MessageRoutedTo({ message, onOpen, onShowReply }: {
  message: CanonicalRoomMessage; onOpen?: (taskId: string) => void; onShowReply?: (taskId: string) => void;
}): ReactElement | null {
  const { tasks } = useLiveEvents();
  const routedTo = message.routedTo;
  const single = !!routedTo && (routedTo.passThrough || routedTo.taskIds.length === 1);
  const { data } = useTaskList(tasks, single ? { kind: "room", roomId: message.roomId } : null);
  if (!routedTo) return null;
  return <RoutedToLink routedTo={routedTo} onOpen={onOpen} onShowReply={onShowReply}
    taskTitle={(id) => data?.tasks.find((t) => t.taskId === id)?.title} />;
}
