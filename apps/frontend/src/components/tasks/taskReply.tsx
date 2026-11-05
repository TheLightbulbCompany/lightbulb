"use client";

// One text box (critique P1-3): the open request pane is steered from its room's composer ("Replying to: …"),
// so the pane says which request is open and each room says whether its composer is on screen.

import { useEffect, useState, useSyncExternalStore, type ReactElement } from "react";
import { X } from "lucide-react";
import { useTask, type TaskSummary, type TasksClient } from "@lightbulb/shared";
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

/**
 * The room composer's "Replying to: <request>" while that request's pane is open (and it isn't a suggestion):
 * sending then posts into the request's room. Removing the chip sends to the room as normal and `onRemove`
 * hands focus back to the composer. `roomId` is null when the room's composer isn't on screen.
 */
export function useTaskReply(roomId: string | null, draft: Pick<RoomComposerProps, "onTextChange">, onRemove?: () => void): {
  chip: ReactElement; composer: Partial<RoomComposerProps>;
} | null {
  useMarkRoomComposer(roomId);
  const taskId = useOpenTaskId();
  // Optional: a room rendered outside the live provider (tests, the share page) simply has no chip.
  const live = useOptionalLiveEvents();
  const { data } = useTask(live?.tasks as TasksClient, live && roomId ? taskId : null);
  const [removed, setRemoved] = useState<string | null>(null);
  // Each send is a fresh composer attempt (its own id), so a sent or failed reply never locks the box.
  const [clientMessageId, setClientMessageId] = useState(() => crypto.randomUUID());
  const [error, setError] = useState<string | null>(null);
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
      onUploadAttachment: undefined, onEditLast: undefined, label: `Reply to ${task.title}`, placeholder: "Steer this request…",
      // It goes to the request's specialist, so the room's "who will reply" line would name the wrong agent.
      showRespondHint: false,
      onSubmit: async (request) => {
        try {
          await sendToTask(live.messaging, task, request.text, "composer");
          setError(null);
          draft.onTextChange("");
        } catch {
          setError("That didn't go through. Try again.");
        } finally {
          setClientMessageId(crypto.randomUUID());
        }
      },
    },
  };
}
