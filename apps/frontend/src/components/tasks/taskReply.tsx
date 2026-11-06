"use client";

// One text box (critique P1-3): the open request pane is steered from its room's composer ("Replying to: …"),
// so the pane says which request is open and each room says whether its composer is on screen.

import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type ReactElement } from "react";
import { X } from "lucide-react";
import { putTaskSummary, useTask, useTaskCache, type TaskSummary, type TasksClient } from "@lightbulb/shared";
import type { RoomComposerProps } from "@/components/messaging/RoomComposer";
import { Button } from "@/components/ui/button";
import { useOptionalLiveEvents, type LiveEventsContextValue } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";

let openTaskId: string | null = null;
const composerRooms = new Map<string, number>();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l); }; };

/** The request pane on screen says so while it is open. */
export function useMarkTaskPaneOpen(taskId: string): void {
  useEffect(() => {
    openTaskId = taskId;
    emit();
    return () => { if (openTaskId === taskId) { openTaskId = null; emit(); } };
  }, [taskId]);
}

export const useOpenTaskId = (): string | null => useSyncExternalStore(subscribe, () => openTaskId, () => null);

let steerTaskId: string | null = null;

/** The open pane says whether its request takes the room composer (UX audit 2, P0): Working and Needs you do; a Done
 *  one only after the pane's Follow up; a Closed one never. Opening a request to look never captures the next message. */
export function useMarkTaskSteerable(taskId: string, on: boolean): void {
  useEffect(() => {
    if (!on) return;
    steerTaskId = taskId;
    emit();
    return () => { if (steerTaskId === taskId) { steerTaskId = null; emit(); } };
  }, [taskId, on]);
}

const useSteerTaskId = (): string | null => useSyncExternalStore(subscribe, () => steerTaskId, () => null);

/** A room's composer, while it is on screen (null: not shown). */
export function useMarkRoomComposer(roomId: string | null): void {
  useEffect(() => {
    if (!roomId) return;
    composerRooms.set(roomId, (composerRooms.get(roomId) ?? 0) + 1);
    emit();
    return () => {
      const n = (composerRooms.get(roomId) ?? 1) - 1;
      if (n) composerRooms.set(roomId, n); else composerRooms.delete(roomId);
      emit();
    };
  }, [roomId]);
}

export const useRoomComposerShown = (roomId: string | null | undefined): boolean =>
  useSyncExternalStore(subscribe, () => !!roomId && composerRooms.has(roomId), () => false);

/** Posts into the request's own room (what the pane's old steer box did): plain text, no mentions or reply thread. */
export async function sendToTask(messaging: LiveEventsContextValue["messaging"], task: Pick<TaskSummary, "taskId" | "taskRoomId" | "status">,
  text: string, source: "composer" | "note"): Promise<void> {
  await messaging.sendMessage(task.taskRoomId, {
    clientMessageId: crypto.randomUUID(), text, mentions: [], replyToMessageId: null, threadRootMessageId: null,
  });
  capture("task_steer_sent", { taskId: task.taskId, status: task.status, source });
}

/** After a steer, a finished request reads Working (with Stop) at once, not "Done" until the revised result lands
 *  (dev walk N-F4); the next task.changed replaces this guess with the server's row. A request that waits on a person
 *  stays as it is: typed text never answers a question. */
export function useShowSteered(tasks: Pick<TasksClient, "cacheScope"> | undefined): (task: TaskSummary) => void {
  const { cache, mutate } = useTaskCache();
  return useCallback((task: TaskSummary) => {
    if (!tasks || task.status !== "idle") return;
    void putTaskSummary(cache, mutate, { ...task, status: "working", shownGroup: "working", watching: false, updatedAt: new Date().toISOString() }, tasks);
  }, [tasks, cache, mutate]);
}

/**
 * The room composer's "Replying to: <request>" while that request's pane is open and steerable (useMarkTaskSteerable):
 * sending then posts into the request's room. Removing the chip sends to the room as normal and `onRemove`
 * hands focus back to the composer. `roomId` is null when the room's composer isn't on screen.
 */
export function useTaskReply(roomId: string | null, draft: Pick<RoomComposerProps, "text" | "onTextChange">, onRemove?: () => void): {
  chip: ReactElement; composer: Partial<RoomComposerProps>;
} | null {
  useMarkRoomComposer(roomId);
  const taskId = useSteerTaskId();
  // Optional: a room rendered outside the live provider (tests, the share page) simply has no chip.
  const live = useOptionalLiveEvents();
  const { data } = useTask(live?.tasks as TasksClient, live && roomId ? taskId : null);
  const showSteered = useShowSteered(live?.tasks);
  const [removed, setRemoved] = useState<string | null>(null);
  // Each send is a fresh composer attempt (its own id), so a sent or failed reply never locks the box.
  const [clientMessageId, setClientMessageId] = useState(() => crypto.randomUUID());
  const [error, setError] = useState<string | null>(null);
  const text = useRef(draft.text);
  text.current = draft.text;
  useEffect(() => { setRemoved(null); setError(null); }, [taskId]);
  const task = data?.task;
  if (!live || !roomId || !task || task.taskId !== taskId || task.originRoomId !== roomId || removed === task.taskId) return null;
  return {
    chip: <div className="flex min-w-0 items-center gap-1 px-1 text-ui text-muted-foreground">
      <span className="min-w-0 truncate">Replying to: <span className="font-semibold text-foreground">{task.title}</span></span>
      <Button type="button" variant="ghost" size="icon-sm" aria-label={`Stop replying to ${task.title}`} title="Send to the room instead"
        onClick={() => { setRemoved(task.taskId); onRemove?.(); }}><X aria-hidden="true" /></Button>
      {error && <span role="alert" className="text-err">{error}</span>}
    </div>,
    composer: {
      clientMessageId, sendState: "idle", members: [], sessionTargets: [], onRetry: undefined, onReconcile: undefined,
      onUploadAttachment: undefined, onEditLast: undefined, label: `Reply to ${task.title}`, placeholder: "Steer this task…",
      // It goes to the request's specialist, so the room's "who will reply" line would name the wrong agent.
      showRespondHint: false,
      onSubmit: async (request) => {
        // The box (and the room's stored draft) empties now, as a room send does; a failure gives the text back.
        draft.onTextChange("");
        try {
          await sendToTask(live.messaging, task, request.text, "composer");
          showSteered(task);
          setError(null);
        } catch {
          if (!text.current.trim()) draft.onTextChange(request.text);
          setError("That didn't go through. Try again.");
        } finally {
          setClientMessageId(crypto.randomUUID());
        }
      },
    },
  };
}
