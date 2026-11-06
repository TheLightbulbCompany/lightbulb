// SWR hooks over the task routes, and the `task.changed` refresh (ids only, I1): the client
// refetches through the authorized API; a task it can no longer see is dropped quietly.

import { useCallback, useMemo, useState } from "react";
import useSWR, { useSWRConfig, type Cache, type ScopedMutator } from "swr";

import { ApiError } from "../api/error";
import type { TaskChanged } from "../live/types";
import { scopeParam, type TasksClient } from "./client";
import type { QuestionView, TaskDetail, TaskScope, TaskSummary } from "./types";
import { resolveCeoAgentId } from "./view";

type TaskList = { tasks: TaskSummary[]; next: string | null };

/** List keys carry the workspace: `scope=mine` in one org must never serve another org's list. */
const listPrefix = (client: Pick<TasksClient, "cacheScope">) => `tasks:${client.cacheScope}:`;
export const taskListKey = (client: Pick<TasksClient, "cacheScope">, scope: TaskScope) => `${listPrefix(client)}${scopeParam(scope)}`;
/** Task ids are UUIDs, unique across workspaces; a task the viewer lost is dropped on its 403. */
export const taskKey = (taskId: string) => `task:${taskId}`;
export const questionsKey = (taskId: string) => `task-questions:${taskId}`;

const listKeys = (cache: Cache, client: Pick<TasksClient, "cacheScope">) =>
  [...cache.keys()].filter((k) => k.startsWith(listPrefix(client)));

/** The first page of a scope (50, tasks.py). Older pages load on demand (`useOlderTasks`); a card whose task
 *  isn't on it reads it by id. `null` scope = no request (switch off or nothing to show). */
export function useTaskList(client: TasksClient, scope: TaskScope | null) {
  return useSWR<TaskList>(scope ? taskListKey(client, scope) : null, () => client.list(scope!), { shouldRetryOnError: false });
}

/** "Load older" for a list: pages after the first, fetched only when asked. They live in the SWR cache under the
 *  list's own prefix (`…:older`), so `putTaskSummary` and `task.changed` (`refreshTask`) update their rows too. */
export function useOlderTasks(client: TasksClient, first: TaskList | undefined, scope: TaskScope | null) {
  const key = scope ? `${taskListKey(client, scope)}:older` : null;
  const { data: older, mutate } = useSWR<TaskList>(key, null); // no fetcher: only loadOlder fills it
  const next = older ? older.next : first?.next ?? null;
  const [loading, setLoading] = useState(false);
  const loadOlder = useCallback(async () => {
    if (!scope || !next) return;
    setLoading(true);
    try {
      const page = await client.list(scope, next);
      await mutate((o) => ({ next: page.next, tasks: [...(o?.tasks ?? []), ...page.tasks] }), { revalidate: false });
    } finally { setLoading(false); }
  }, [client, scope, next, mutate]);
  const seen = new Set((first?.tasks ?? []).map((t) => t.taskId));
  return { older: (older?.tasks ?? []).filter((t) => !seen.has(t.taskId)), hasOlder: !!next, loading, loadOlder };
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

/** A Needs-you read (`/tasks?needsYou=true`, the rail badge and Home's needs-you line) is cached under its scope's list
 *  key plus this suffix. Its membership is the server's filter, so an event never edits it in place: it is re-read. */
export const NEEDS_YOU_KEY_SUFFIX = ":needs-you";

/** One trailing re-read per burst: a run emits many events (attempt, question, effect rows) in a row. */
export const NEEDS_YOU_COALESCE_MS = 750;
const pendingNeedsYou = new WeakMap<Cache, { timer: ReturnType<typeof setTimeout>; deps: RefreshDeps }>();

/** Re-read every Needs-you cut once, NEEDS_YOU_COALESCE_MS after the first event of a burst (the latest deps win). */
function scheduleNeedsYou(deps: RefreshDeps): void {
  const pending = pendingNeedsYou.get(deps.cache);
  if (pending) { pending.deps = deps; return; }
  const entry = { deps, timer: setTimeout(() => {
    pendingNeedsYou.delete(deps.cache);
    const { cache, mutate, client } = entry.deps;
    for (const k of listKeys(cache, client)) if (k.endsWith(NEEDS_YOU_KEY_SUFFIX)) void mutate(k);
  }, NEEDS_YOU_COALESCE_MS) };
  pendingNeedsYou.set(deps.cache, entry);
}

export async function refreshTask(deps: RefreshDeps, changed: TaskChanged): Promise<void> {
  // Every change can move a request into or out of Needs you (a new approval, an answer, a stop): re-read those cuts
  // after the event that updates the room's cards, so the badge never waits for Activity to open (dev walk W8-3).
  scheduleNeedsYou(deps);
  await refreshRows(deps, changed);
}

async function refreshRows({ cache, mutate, client }: RefreshDeps, { taskId, part, version }: TaskChanged): Promise<void> {
  const lists = listKeys(cache, client);
  const detail = cache.get(taskKey(taskId))?.data as TaskDetail | undefined;
  // An attempt, Question or Effect row counts its own versions from 1: only an open detail cares about it.
  if (part !== "task") {
    if (detail) await refetch({ cache, mutate, client }, taskId, lists);
    return;
  }
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
  await refetch({ cache, mutate, client }, taskId, lists);
}

async function refetch({ cache, mutate, client }: RefreshDeps, taskId: string, lists: string[]): Promise<void> {
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

export type DirectoryAgent = { id: string; name?: string | null; role?: string | null; reportsTo?: string | null; origin?: string | null };

/** Names by agent id (task specialists are agent ids) and the front agent's id (`resolveCeoAgentId`), which displays
 *  as "Lightbulb" with the switch on. Pure, so the web app can derive it from its existing `/teams/agents` read. */
export function agentDirectory(agents: readonly DirectoryAgent[]): { names: ReadonlyMap<string, string>; frontAgentId: string | undefined } {
  const frontAgentId = resolveCeoAgentId(agents);
  // The front agent's roster name ("CEO") never shows: it is Lightbulb everywhere.
  const names = new Map(agents.filter((a) => a.name).map((a) => [a.id, a.id === frontAgentId ? "Lightbulb" : a.name!]));
  return { names, frontAgentId };
}

/** `agentDirectory` over one SWR read shared by every caller with the same `key` (the app passes its own loader:
 *  `/teams/agents`). `null` key = no request. */
export function useAgentDirectory(key: string | null, load: () => Promise<readonly DirectoryAgent[]>): { names: ReadonlyMap<string, string>; frontAgentId: string | undefined } {
  const { data } = useSWR(key ? ["agent-names", key] : null, load, { shouldRetryOnError: false, dedupingInterval: 60_000 });
  return useMemo(() => agentDirectory(data ?? []), [data]);
}
