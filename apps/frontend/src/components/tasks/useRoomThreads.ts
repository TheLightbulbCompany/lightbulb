"use client";

// Threads for a badge: a room's (the About button's dot) or the workspace's (the Home rail). Both count Home's
// Needs you cut (homeSections) of the same list Home renders, so a badge never disagrees with the rows under it.
// No request at all while the switch is off or outside the live provider (tests).

import { useTaskList, type TaskScope, type TasksClient } from "@lightbulb/shared";
import { homeSections } from "./homeSections";
import { useOptionalLiveEvents, useSuperAgent } from "@/hooks/useLiveEvents";

const NO_CLIENT = { cacheScope: "" } as TasksClient; // never called: its key is always null
const WORKSPACE: TaskScope = { kind: "workspace" };

function useNeedsYouIn(scope: TaskScope | null) {
  const live = useOptionalLiveEvents();
  const on = useSuperAgent();
  const { data } = useTaskList(live?.tasks ?? NO_CLIENT, on && live ? scope : null);
  const tasks = data?.tasks ?? [];
  return { tasks, waitingOnMe: homeSections(tasks).needsYou.length };
}

export function useRoomThreads(roomId: string | null | undefined) {
  return useNeedsYouIn(roomId ? { kind: "room", roomId } : null);
}

/** Home's Needs you count (first page of the workspace list, as Home shows before "Load older"). */
export function useHomeNeedsYou(): number {
  return useNeedsYouIn(WORKSPACE).waitingOnMe;
}
