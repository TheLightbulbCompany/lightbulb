// SWR hooks over the task routes, and the `task.changed` refresh (ids only, I1): the client
// refetches through the authorized API; a task it can no longer see is dropped quietly.

import { useCallback, useMemo } from "react";
import useSWR, { useSWRConfig, type Cache, type ScopedMutator } from "swr";

import { ApiError } from "../api/error";
import type { TaskChanged } from "../live/types";
import { scopeParam, type TasksClient } from "./client";
import type { QuestionView, TaskDetail, TaskScope, TaskSummary } from "./types";

type TaskList = { tasks: TaskSummary[]; next: string | null };

/** List keys carry the workspace: `scope=mine` in one org must never serve another org's list. */
const listPrefix = (client: Pick<TasksClient, "cacheScope">) => `tasks:${client.cacheScope}:`;
export const taskListKey = (client: Pick<TasksClient, "cacheScope">, scope: TaskScope) => `${listPrefix(client)}${scopeParam(scope)}`;
/** Task ids are UUIDs, unique across workspaces; a task the viewer lost is dropped on its 403. */
export const taskKey = (taskId: string) => `task:${taskId}`;
export const questionsKey = (taskId: string) => `task-questions:${taskId}`;

const listKeys = (cache: Cache, client: Pick<TasksClient, "cacheScope">) =>
  [...cache.keys()].filter((k) => k.startsWith(listPrefix(client)));

/** `null` scope = no request (switch off or nothing to show). */
export function useTaskList(client: TasksClient, scope: TaskScope | null) {
  return useSWR<TaskList>(scope ? taskListKey(client, scope) : null, () => client.list(scope!), { shouldRetryOnError: false });
}

/** `null` id = no request. */
/** `null` id = no request. No retry on an error: a stale link's 404 would otherwise refetch forever. */
export function useTask(client: TasksClient, taskId: string | null) {
  return useSWR<TaskDetail>(taskId ? taskKey(taskId) : null, () => client.get(taskId!), { shouldRetryOnError: false });
}

/** The viewer-specific question views of one task (`GET /tasks/{id}/questions`); `null` id = no request. */
export function useTaskQuestions(client: TasksClient, taskId: string | null) {
  return useSWR<{ items: QuestionView[] }>(taskId ? questionsKey(taskId) : null, () => client.questions(taskId!), { shouldRetryOnError: false });
}

/** The SWR cache and mutator the task hooks share; apps use it for their own action hooks. */
export function useTaskCache(): { cache: Cache; mutate: ScopedMutator } {
  const { cache, mutate } = useSWRConfig();
  return { cache, mutate };
}

/** Write a fresh summary (e.g. an action's response) into every cached list and the detail. */
export async function putTaskSummary(cache: Cache, mutate: ScopedMutator, task: TaskSummary, client: Pick<TasksClient, "cacheScope">): Promise<void> {
  await Promise.all([
    mutate<TaskDetail>(taskKey(task.taskId), (d) => (d ? { ...d, task } : d), { revalidate: false }),
    ...listKeys(cache, client).map((k) => mutate<TaskList>(
      k, (d) => (d ? { ...d, tasks: d.tasks.map((t) => (t.taskId === task.taskId ? task : t)) } : d), { revalidate: false },
    )),
  ]);
}

export interface RefreshDeps { cache: Cache; mutate: ScopedMutator; client: TasksClient }

export async function refreshTask({ cache, mutate, client }: RefreshDeps, { taskId, version }: TaskChanged): Promise<void> {
  const lists = listKeys(cache, client);
  const detail = cache.get(taskKey(taskId))?.data as TaskDetail | undefined;
  const known = [
    detail?.task.version,
    ...lists.map((k) => (cache.get(k)?.data as TaskList | undefined)?.tasks.find((t) => t.taskId === taskId)?.version),
  ].filter((v): v is number => typeof v === "number");

  // A task no cached list holds may belong in one: revalidate the lists (the server filters by can_view).
  if (!known.length) {
    await Promise.all(lists.map((k) => mutate(k)));
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
        ...lists.map((k) => mutate<TaskList>(
          k, (d) => (d ? { ...d, tasks: d.tasks.filter((t) => t.taskId !== taskId) } : d), { revalidate: false },
        )),
      ]);
    }
    return; // anything else: the next event or focus revalidation catches up
  }
  await mutate(taskKey(taskId), fresh, { revalidate: false });
  await putTaskSummary(cache, mutate, fresh.task, client);
  if (cache.get(questionsKey(taskId))) await mutate(questionsKey(taskId));
}

/** The handler a live provider calls for each `task.changed`. */
export function useTaskChangedRefresh(client: TasksClient): (payload: TaskChanged) => void {
  const { cache, mutate } = useSWRConfig();
  return useCallback((payload: TaskChanged) => void refreshTask({ cache, mutate, client }, payload), [cache, mutate, client]);
}

/** The workspace's agent names by id, one SWR read shared by every caller with the same `key` (each app passes its
 *  own loader: web the teams API, mobile `/teams/agents`). `null` key = no request. */
export function useAgentNames(key: string | null, load: () => Promise<readonly { id: string; name?: string | null }[]>): ReadonlyMap<string, string> {
  const { data } = useSWR(key ? ["agent-names", key] : null, load, { shouldRetryOnError: false, dedupingInterval: 60_000 });
  return useMemo(() => new Map((data ?? []).filter((a) => a.name).map((a) => [a.id, a.name!])), [data]);
}
