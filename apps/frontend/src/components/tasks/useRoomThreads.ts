"use client";

// Threads for a header badge: a room's (the Threads button's waiting dot) or the workspace's (the Work rail).
// No request at all while the switch is off or outside the live provider (tests).

import useSWR from "swr";
import { useTaskList, type TasksClient } from "@lightbulb/shared";
import { useOptionalLiveEvents, useSuperAgent } from "@/hooks/useLiveEvents";

const NO_CLIENT = { cacheScope: "" } as TasksClient; // never called: its key is always null

export function useRoomThreads(roomId: string | null | undefined) {
  const live = useOptionalLiveEvents();
  const on = useSuperAgent();
  const { data } = useTaskList(live?.tasks ?? NO_CLIENT, on && live && roomId ? { kind: "room", roomId } : null);
  const tasks = data?.tasks ?? [];
  return { tasks, waitingOnMe: tasks.filter((t) => t.shownGroup === "waiting_on_you").length };
}

/** Threads with a question waiting on the viewer, workspace-wide, from the needs-you index (one indexed query,
 *  not the thread list). Counts threads, not questions. */
export function useThreadsWaitingOnMe(): number {
  const live = useOptionalLiveEvents();
  const on = useSuperAgent();
  const { data } = useSWR(on && live ? ["needs-you", live.tasks.cacheScope] : null, () => live!.tasks.needsYou(), { shouldRetryOnError: false });
  return new Set((data?.items ?? []).map((q) => q.taskId)).size;
}
