"use client";

// Threads for a header badge: a room's (the Threads button's waiting dot) or the workspace's (the Work rail).
// No request at all while the switch is off or outside the live provider (tests).

import { useTaskList, type TaskScope, type TasksClient } from "@lightbulb/shared";
import { useOptionalLiveEvents, useSuperAgent } from "@/hooks/useLiveEvents";

const NO_CLIENT = { cacheScope: "" } as TasksClient; // never called: its key is always null

export function useThreads(scope: TaskScope | null) {
  const live = useOptionalLiveEvents();
  const on = useSuperAgent();
  const { data } = useTaskList(live?.tasks ?? NO_CLIENT, on && live ? scope : null);
  const tasks = data?.tasks ?? [];
  return { tasks, waitingOnMe: tasks.filter((t) => t.shownGroup === "waiting_on_you").length };
}

export function useRoomThreads(roomId: string | null | undefined) {
  return useThreads(roomId ? { kind: "room", roomId } : null);
}
