"use client";

// Thread cards under a Lightbulb message (`taskRefs`), the Suggested block, and the
// "Sent to …" line under a person's message (`routedTo`). Only rendered with the switch on.
// One shared room list read (SWR dedupes it across every message in the room).

import { useState, type ReactElement } from "react";
import { useSWRConfig } from "swr";
import { putTaskSummary, useTaskList, type CanonicalRoomMessage, type TaskRef, type TaskSummary } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";
import { TaskCard } from "./TaskCard";
import { errorCopy, useTaskActions } from "./useTaskActions";

export function MessageTasks({ message, viewerId, onOpen }: {
  message: CanonicalRoomMessage; viewerId: string; onOpen?: (taskId: string) => void;
}): ReactElement | null {
  const { tasks } = useLiveEvents();
  const refs = message.taskRefs ?? [];
  const { data } = useTaskList(tasks, refs.length ? { kind: "room", roomId: message.roomId } : null);
  if (!refs.length) return null;
  const byId = new Map((data?.tasks ?? []).map((t) => [t.taskId, t]));
  // Before the list loads, render nothing rather than a row of "isn't available".
  if (!data) return null;
  const started = refs.filter((r) => r.role === "started");
  const suggested = refs.filter((r) => r.role === "suggested");
  return <div aria-label="Threads in this message">
    {started.map((r) => <TaskCard key={r.taskId} task={byId.get(r.taskId)} taskId={r.taskId} viewerId={viewerId} onOpen={onOpen} />)}
    {suggested.length > 0 && <SuggestedBlock refs={suggested} taskById={byId} viewerId={viewerId} onOpen={onOpen} />}
  </div>;
}

export function SuggestedBlock({ refs, taskById, viewerId, onOpen }: {
  refs: TaskRef[]; taskById: ReadonlyMap<string, TaskSummary>; viewerId: string; onOpen?: (taskId: string) => void;
}): ReactElement {
  const { tasks } = useLiveEvents();
  const { cache, mutate } = useSWRConfig();
  const { act, refetch } = useTaskActions("suggested");
  const [pending, setPending] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const proposed = refs.map((r) => taskById.get(r.taskId)).filter((t): t is TaskSummary => t?.status === "proposed");

  const one = async (task: TaskSummary, verb: "start" | "dismiss") => {
    setPending(task.taskId);
    const res = await act(task, verb);
    setPending(null);
    setErrors((e) => ({ ...e, [task.taskId]: res.ok ? "" : res.message }));
  };
  const all = async () => {
    setPending("all");
    try {
      const res = await tasks.startMany(proposed.map((t) => t.taskId)) as { tasks?: TaskSummary[]; failed?: { taskId: string }[] } | null;
      for (const t of res?.tasks ?? []) await putTaskSummary(cache, mutate, t, tasks);
      const failed = new Set((res?.failed ?? []).map((f) => f.taskId));
      setErrors(Object.fromEntries([...failed].map((id) => [id, "Couldn't start this one. Try again."])));
      if (!res?.tasks) await Promise.all(proposed.map((t) => refetch(t.taskId)));
      capture("task_suggestion_started", { source: "start_all", count: proposed.length });
    } catch (error) {
      setErrors(Object.fromEntries(proposed.map((t) => [t.taskId, errorCopy(error)])));
    } finally {
      setPending(null);
    }
  };

  return <section aria-label="Suggested" className="mt-2 max-w-xl">
    <div className="flex items-center gap-2">
      <h4 className="label-caps">Suggested</h4>
      {proposed.length >= 2 && <Button type="button" size="sm" variant="ghost" disabled={pending !== null} onClick={() => void all()}>Start all</Button>}
    </div>
    {refs.map((r) => {
      const task = taskById.get(r.taskId);
      const waiting = task?.status === "proposed";
      return <TaskCard key={r.taskId} task={task} taskId={r.taskId} viewerId={viewerId} onOpen={onOpen} hideAction={waiting}>
        {waiting && <div className="relative z-10 mt-2 flex items-center gap-2">
          <Button type="button" size="sm" disabled={pending !== null} onClick={() => void one(task, "start")}>Start</Button>
          <Button type="button" size="sm" variant="ghost" disabled={pending !== null} onClick={() => void one(task, "dismiss")}>Dismiss</Button>
        </div>}
        {errors[r.taskId] && <p role="alert" className="relative z-10 mt-1 text-ui text-err">{errors[r.taskId]}</p>}
      </TaskCard>;
    })}
  </section>;
}

/** "Sent to Workshop signup app ›" (one thread, opens it) or "Sent to 2 threads, 1 suggested ›" (scrolls to the reply). */
export function RoutedToLink({ routedTo, taskTitle, onOpen, onShowReply }: {
  routedTo: NonNullable<CanonicalRoomMessage["routedTo"]>;
  taskTitle?: (taskId: string) => string | undefined;
  onOpen?: (taskId: string) => void;
  onShowReply?: () => void;
}): ReactElement | null {
  const { taskIds, started, suggested, passThrough } = routedTo;
  if (!taskIds.length) return null;
  const single = passThrough || taskIds.length === 1;
  const label = single
    ? `Sent to ${taskTitle?.(taskIds[0]) ?? "a thread"}`
    : [`Sent to ${started} ${started === 1 ? "thread" : "threads"}`, suggested ? `${suggested} suggested` : ""].filter(Boolean).join(", ");
  return <button type="button" className="mt-0.5 text-ui text-muted-foreground hover:text-foreground hover:underline"
    onClick={() => (single ? onOpen?.(taskIds[0]) : onShowReply?.())}>{label} ›</button>;
}

/** `RoutedToLink` for a person's message, with the thread's title from the room's list. */
export function MessageRoutedTo({ message, onOpen, onShowReply }: {
  message: CanonicalRoomMessage; onOpen?: (taskId: string) => void; onShowReply?: () => void;
}): ReactElement | null {
  const { tasks } = useLiveEvents();
  const routedTo = message.routedTo;
  const single = !!routedTo && (routedTo.passThrough || routedTo.taskIds.length === 1);
  const { data } = useTaskList(tasks, single ? { kind: "room", roomId: message.roomId } : null);
  if (!routedTo) return null;
  return <RoutedToLink routedTo={routedTo} onOpen={onOpen} onShowReply={onShowReply}
    taskTitle={(id) => data?.tasks.find((t) => t.taskId === id)?.title} />;
}
