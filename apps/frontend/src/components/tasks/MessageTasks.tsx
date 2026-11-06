"use client";

// Request cards under a Lightbulb message (`taskRefs`) and the "Sent to …" line under a person's message (`routedTo`). Only rendered with the switch on.
// One shared room list read (SWR dedupes it across every message in the room).

import { useEffect, useState, type ReactElement } from "react";
import { Check, ChevronRight, Copy } from "lucide-react";
import { chipState, specialistLabel, useTask, useTaskList, type CanonicalRoomMessage, type ChipMark, type TaskSummary } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { cn } from "@/lib/utils";
import { TaskOutputs } from "@/components/creations/TaskOutputs";
import { copyText } from "@/components/messaging/messageLinks";
import { plainCta, resultPiece } from "@/components/creations/docText";

export { resultPiece };
import { TaskCard } from "./TaskCard";

export function MessageTasks({ message, viewerId, onOpen, ackedTasks }: {
  message: CanonicalRoomMessage; viewerId: string; onOpen?: (taskId: string) => void;
  /** "thread|taskId" for each task whose ack chip is in this timeline ("" = main list). Absent: none known. */
  ackedTasks?: ReadonlySet<string>;
}): ReactElement | null {
  const { tasks } = useLiveEvents();
  const refs = message.taskRefs ?? [];
  const { data, error, mutate } = useTaskList(tasks, refs.length ? { kind: "room", roomId: message.roomId } : null);
  if (!refs.length) return null;
  // A finish message that carries a short result whole (walk 15: an Instagram post) offers it to paste.
  const piece = message.kind === "task_line" ? resultPiece(message.text) : null;
  const copy = piece ? <CopyResult text={piece} /> : null;
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
  if (copy && !data) return copy;
  // One chip per task (dev walk 13, W13-1/W12-1): the Router's ack carries it with a live dot, ✓ Done once finished;
  // the one-line finish post carries only what it made. A result post or a question keeps the full card.
  const Ref = !message.kind || message.kind === "task_line" ? RefChip : RefCard;
  return <div aria-label="Tasks in this message">
    {copy}
    {refs.map((r) => <Ref key={r.taskId} taskId={r.taskId} listed={byId.get(r.taskId)} viewerId={viewerId} onOpen={onOpen}
      withOutputs={message.kind === "task_line"} acked={ackedTasks?.has(`${message.threadRootMessageId ?? ""}|${r.taskId}`) ?? false} />)}
  </div>;
}

const MARK_TEXT: Record<ChipMark, string> = {
  working: "text-info", waiting: "text-warn", done: "text-ok", failed: "text-err", closed: "text-muted-foreground",
};

/** One task, one chip (dev walk 12, W12-1): its title, specialist and live state, "Working · 0:38" → "Done · 1:02"
 *  or "Didn't finish", read from the room's one task list. The finish line (`withOutputs`) carries what the task made,
 *  and the task chip only where it is the way in (W13-1): the ack's chip is beside it (`acked`, same thread) and the
 *  task made something and didn't fail. A failed task's finish line always keeps its chip, so Retry is one click away.
 *  The task comes only from reads the server filters by who may view it (the room list, else `GET /tasks/{id}`): no
 *  read, no chip and no outputs. */
function RefChip({ taskId, listed, viewerId, onOpen, withOutputs, acked = false }: {
  taskId: string; listed: TaskSummary | undefined; viewerId: string; onOpen?: (taskId: string) => void; withOutputs?: boolean; acked?: boolean;
}): ReactElement | null {
  const { tasks, agentNames } = useLiveEvents();
  const { data } = useTask(tasks, listed ? null : taskId);
  const task = listed ?? data?.task;
  const [now, setNow] = useState(() => Date.now());
  const state = task ? chipState(task, viewerId, now) : null;
  const ticking = state?.mark === "working" && !!state.time;
  // One clock (W16-11): the moment it starts counting it reads the time now, never the mount's (0:00 → 0:03).
  useEffect(() => {
    if (!ticking) return;
    const first = window.setTimeout(() => setNow(Date.now()), 0);
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => { window.clearTimeout(first); window.clearInterval(timer); };
  }, [ticking]);
  if (!task || !state) return null;
  // Done shows the run's stored length (endedAt − startedAt) on every read: one chip never says 0:22 and then 0:21 (W17).
  const time = state.time;
  const outputs = !withOutputs ? null : <TaskOutputs task={task} max={2} onOpenTask={() => onOpen?.(taskId)} />;
  // Beside its ack the chip is never repeated (walk 15: a task that made no file showed the same chip twice).
  if (withOutputs && acked && task.status !== "failed") return outputs;
  const who = task.specialist ? specialistLabel(task.specialist, agentNames) : null;
  const status = time ? `${state.word} · ${time}` : state.word;
  return <div>
    <button type="button" data-task-chip={taskId} data-mark={state.mark} onClick={() => onOpen?.(taskId)}
      aria-label={`Open ${task.title}, ${status}`}
      className="mt-1 inline-flex max-w-full items-center gap-1.5 rounded-md border border-border px-2 py-0.5 text-ui hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring">
      <span className="min-w-0 truncate font-semibold">{task.title}</span>
      {who && <span className="shrink-0 text-muted-foreground">· {who}</span>}
      <span className={cn("inline-flex shrink-0 items-center gap-1 tabular-nums", MARK_TEXT[state.mark])}>
        {state.mark === "done" ? <Check aria-hidden="true" className="size-3.5" strokeWidth={2} />
          : <span aria-hidden="true" className={cn("size-1.5 rounded-full bg-current", state.mark === "working" && "motion-safe:animate-pulse")} />}
        {status}
      </span>
      <ChevronRight aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
    </button>
    {outputs}
  </div>;
}

/** Copy, with ✓ for 2 s. The label never changes, so the button never shifts. */
export function CopyResult({ text }: { text: string }): ReactElement {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return <button type="button" onClick={() => void copyText(plainCta(text)).then(setCopied)} aria-label={copied ? "Copied" : "Copy"}
    className="mt-1 inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-ui text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-ring">
    {copied ? <Check aria-hidden="true" className="size-3.5 text-ok" strokeWidth={2} /> : <Copy aria-hidden="true" className="size-3.5" />}
    Copy
  </button>;
}

/** A card whose task the room list doesn't hold (older than its pages, or another scope) reads it by id. */
function RefCard({ taskId, listed, viewerId, onOpen }: {
  taskId: string; listed: TaskSummary | undefined; viewerId: string; onOpen?: (taskId: string) => void; withOutputs?: boolean; acked?: boolean;
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
