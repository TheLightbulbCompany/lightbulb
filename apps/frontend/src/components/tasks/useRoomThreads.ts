"use client";

// Needs you for a badge: a room's (the About button's dot) or the workspace's (the Home rail and Home's Needs you
// rows). One complete read, `GET /tasks?needsYou=true` (every request waiting on the viewer or ready for their
// review, unpaged), cut by the same `shownGroup` rule Home renders (homeSections), so a badge never disagrees with
// the rows under it. No request at all while the switch is off or outside the live provider (tests).

import useSWR from "swr";
import { scopeParam, taskListKey, type TaskScope, type TaskSummary, type TasksClient } from "@lightbulb/shared";
import { useOptionalLiveEvents, useSuperAgent } from "@/hooks/useLiveEvents";
import { useApi } from "@/lib/api";
import { homeSections } from "./homeSections";

const WORKSPACE: TaskScope = { kind: "workspace" };
const NONE: TaskSummary[] = [];

/** The scope's Needs you requests. Keyed under the scope's list prefix, so `task.changed` revalidates it (a new
 *  ask arrives) and rewrites its rows in place (an answered one leaves the cut). */
export function useNeedsYouTasks(scope: TaskScope | null): { tasks: TaskSummary[]; loaded: boolean; error: unknown; retry: () => void } {
  const live = useOptionalLiveEvents();
  const on = useSuperAgent();
  const api = useApi();
  const client: Pick<TasksClient, "cacheScope"> | undefined = live?.tasks;
  const key = on && client && scope ? `${taskListKey(client, scope)}:needs-you` : null;
  const { data, error, mutate } = useSWR<{ tasks: TaskSummary[]; next: string | null }>(key,
    () => api.get(`/tasks?scope=${encodeURIComponent(scopeParam(scope!))}&needsYou=true`) as Promise<{ tasks: TaskSummary[]; next: string | null }>,
    { shouldRetryOnError: false });
  return { tasks: data ? homeSections(data.tasks).needsYou : NONE, loaded: !!data, error, retry: () => void mutate() };
}

export function useRoomThreads(roomId: string | null | undefined) {
  return { waitingOnMe: useNeedsYouTasks(roomId ? { kind: "room", roomId } : null).tasks.length };
}

/** Home's Needs you count: the rail badge. */
export function useHomeNeedsYou(): number {
  return useNeedsYouTasks(WORKSPACE).tasks.length;
}
