"use client";

// Needs you for a badge: the workspace's (the Home rail and Home's Needs you rows) and each room's (the About
// dot, its share of the same result). One complete read, `GET /tasks?needsYou=true` (every request waiting on the viewer or ready for their
// review, unpaged), cut by the shared `overviewGroups` Home renders, so a badge never disagrees with
// the rows under it. No request at all while the switch is off or outside the live provider (tests).

import useSWR from "swr";
import { NEEDS_YOU_KEY_SUFFIX, overviewGroups, scopeParam, taskListKey, type TaskScope, type TaskSummary, type TasksClient } from "@lightbulb/shared";
import { useOptionalLiveEvents, useSuperAgent } from "@/hooks/useLiveEvents";
import { homeExtras } from "@/components/activity/activityPage";
import { useActivity } from "@/hooks/useActivity";
import { useFirstMoves } from "@/components/ideas/FirstMoves";
import { useApi } from "@/lib/api";

const WORKSPACE: TaskScope = { kind: "workspace" };

/** The Needs you heading's rows, by the shared four-status cut (waiting on you + ready for review). */
export const needsYouOf = (ts: readonly TaskSummary[]): TaskSummary[] =>
  overviewGroups(ts).find((g) => g.id === "needs_you")?.tasks ?? [];
const NONE: TaskSummary[] = [];

/** The scope's Needs you requests. Keyed under the scope's list prefix with NEEDS_YOU_KEY_SUFFIX, so every
 *  `task.changed` re-reads it (refreshTask): a new ask arrives and an answered one leaves while you sit in a room. */
export function useNeedsYouTasks(scope: TaskScope | null): { tasks: TaskSummary[]; loaded: boolean; error: unknown; retry: () => void } {
  const live = useOptionalLiveEvents();
  const on = useSuperAgent();
  const api = useApi();
  const client: Pick<TasksClient, "cacheScope"> | undefined = live?.tasks;
  const key = on && client && scope ? `${taskListKey(client, scope)}${NEEDS_YOU_KEY_SUFFIX}` : null;
  const { data, error, mutate } = useSWR<{ tasks: TaskSummary[]; next: string | null }>(key,
    () => api.get(`/tasks?scope=${encodeURIComponent(scopeParam(scope!))}&needsYou=true`) as Promise<{ tasks: TaskSummary[]; next: string | null }>,
    { shouldRetryOnError: false });
  return { tasks: data ? needsYouOf(data.tasks) : NONE, loaded: !!data, error, retry: () => void mutate() };
}

/** A room's About dot: the room's share of the one workspace-wide read (the server's room scope is
 *  `audience.roomId`), so N open rooms cost no extra requests. */
export function useRoomThreads(roomId: string | null | undefined) {
  const { tasks } = useNeedsYouTasks(roomId ? WORKSPACE : null);
  return { waitingOnMe: roomId ? tasks.filter((t) => t.audience?.roomId === roomId).length : 0 };
}

/** needsYouCount, the one count (UX audit slice 2): the workspace's Needs you tasks plus the open asks that are not
 *  tasks (a website sign-in, a browser extension, a held message), exactly what Activity's Needs you block lists. The
 *  rail badge, Home's "N need you" line and the tab title all read it; nothing computes its own. Plus "Pick a first
 *  move" while the first run's ideas are undecided.
 *  ponytail: the asks come from Activity's loaded pages (newest first); an ask past them counts once it loads. */
export function useNeedsYouCount(): number {
  const tasks = useNeedsYouTasks(WORKSPACE).tasks.length;
  const { items } = useActivity();
  // First run (UX slice 6): "Pick a first move" is one item while any of the first run's ideas is still open.
  const firstMove = useFirstMoves().length > 0 ? 1 : 0;
  return tasks + homeExtras(items).asks.length + firstMove;
}
