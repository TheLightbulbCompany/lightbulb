"use client";

// Steering a task: the pane's own box posts into the task's room (UX slice 3). The room's composer is never taken
// over by an open task ("Replying to: …" was removed): it always talks to the room.

import { useCallback } from "react";
import { putTaskSummary, useTaskCache, type TaskSummary, type TasksClient } from "@lightbulb/shared";
import type { LiveEventsContextValue } from "@/hooks/useLiveEvents";
import { capture } from "@/lib/analytics";

/** Posts into the task's own room: plain text, no mentions or reply thread. */
export async function sendToTask(messaging: LiveEventsContextValue["messaging"], task: Pick<TaskSummary, "taskId" | "taskRoomId" | "status">,
  text: string, source: "composer" | "note"): Promise<void> {
  await messaging.sendMessage(task.taskRoomId, {
    clientMessageId: crypto.randomUUID(), text, mentions: [], replyToMessageId: null, threadRootMessageId: null,
  });
  capture("task_steer_sent", { taskId: task.taskId, status: task.status, source });
}

/** After a steer, a finished task reads Working (with Stop) at once, not "Done" until the revised result lands
 *  (dev walk N-F4); the next task.changed replaces this guess with the server's row. A task that waits on a person
 *  stays as it is: typed text never answers a question. */
export function useShowSteered(tasks: Pick<TasksClient, "cacheScope"> | undefined): (task: TaskSummary) => void {
  const { cache, mutate } = useTaskCache();
  return useCallback((task: TaskSummary) => {
    if (!tasks || task.status !== "idle") return;
    void putTaskSummary(cache, mutate, { ...task, status: "working", shownGroup: "working", watching: false, updatedAt: new Date().toISOString() }, tasks);
  }, [tasks, cache, mutate]);
}
