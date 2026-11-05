// SWR hooks over the task routes, and the `task.changed` refresh (ids only, I1): the client
// refetches through the authorized API; a task it can no longer see is dropped quietly.

import { useCallback } from "react";
import useSWR, { useSWRConfig, type Cache, type ScopedMutator } from "swr";

import { ApiError } from "../api/error";
import type { TaskChanged } from "../live/types";
import { scopeParam, type TasksClient } from "./client";
import type { TaskDetail, TaskScope, TaskSummary } from "./types";

type TaskList = { tasks: TaskSummary[]; next: string | null };

const LIST_PREFIX = "tasks:";
export const taskListKey = (scope: TaskScope) => `${LIST_PREFIX}${scopeParam(scope)}`;
export const taskKey = (taskId: string) => `task:${taskId}`;

/** `null` scope = no request (switch off or nothing to show). */
export function useTaskList(client: TasksClient, scope: TaskScope | null) {
  return useSWR<TaskList>(scope ? taskListKey(scope) : null, () => client.list(scope!));
}

/** `null` id = no request. */
export function useTask(client: TasksClient, taskId: string | null) {
  return useSWR<TaskDetail>(taskId ? taskKey(taskId) : null, () => client.get(taskId!));
}

/** Write a fresh summary (e.g. an action's response) into every cached list and the detail. */
export async function putTaskSummary(cache: Cache, mutate: ScopedMutator, task: TaskSummary): Promise<void> {
  const listKeys = [...cache.keys()].filter((k) => k.startsWith(LIST_PREFIX));
  await Promise.all([
    mutate<TaskDetail>(taskKey(task.taskId), (d) => (d ? { ...d, task } : d), { revalidate: false }),
    ...listKeys.map((k) => mutate<TaskList>(
      k, (d) => (d ? { ...d, tasks: d.tasks.map((t) => (t.taskId === task.taskId ? task : t)) } : d), { revalidate: false },
    )),
  ]);
}

export interface RefreshDeps { cache: Cache; mutate: ScopedMutator; client: TasksClient }

export async function refreshTask({ cache, mutate, client }: RefreshDeps, { taskId, version }: TaskChanged): Promise<void> {
  const listKeys = [...cache.keys()].filter((k) => k.startsWith(LIST_PREFIX));
  const detail = cache.get(taskKey(taskId))?.data as TaskDetail | undefined;
  const known = [
    detail?.task.version,
    ...listKeys.map((k) => (cache.get(k)?.data as TaskList | undefined)?.tasks.find((t) => t.taskId === taskId)?.version),
  ].filter((v): v is number => typeof v === "number");

  // A task no cached list holds may belong in one: revalidate the lists (the server filters by can_view).
  if (!known.length) {
    await Promise.all(listKeys.map((k) => mutate(k)));
    return;
  }
  if (Math.max(...known) >= version) return;

  let fresh: TaskDetail;
  try {
    fresh = await client.get(taskId);
  } catch (error) {
    if (error instanceof ApiError && (error.status === 403 || error.status === 404)) {
      await Promise.all([
        mutate(taskKey(taskId), undefined, { revalidate: false }),
        ...listKeys.map((k) => mutate<TaskList>(
          k, (d) => (d ? { ...d, tasks: d.tasks.filter((t) => t.taskId !== taskId) } : d), { revalidate: false },
        )),
      ]);
    }
    return; // anything else: the next event or focus revalidation catches up
  }
  await mutate(taskKey(taskId), fresh, { revalidate: false });
  await putTaskSummary(cache, mutate, fresh.task);
}

/** The handler a live provider calls for each `task.changed`. */
export function useTaskChangedRefresh(client: TasksClient): (payload: TaskChanged) => void {
  const { cache, mutate } = useSWRConfig();
  return useCallback((payload: TaskChanged) => void refreshTask({ cache, mutate, client }, payload), [cache, mutate, client]);
}
