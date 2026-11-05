"use client";

// A room's threads for the conversation header (the Threads button's waiting dot) and its
// overview. No request at all while the switch is off or outside the live provider (tests).

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
