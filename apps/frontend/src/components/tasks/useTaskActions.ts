"use client";

// Every button that changes a task goes through here: one POST, the returned summary written into
// the cache, and a 409 (someone else acted first) answered by a refetch, never a second POST.

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { ApiError, putTaskSummary, taskKey, type TaskAction, type TaskSummary, type TaskVerb } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { capture, type AnalyticsEvent } from "@/lib/analytics";

const VERB_EVENTS: Record<TaskVerb, AnalyticsEvent> = {
  start: "task_suggestion_started", dismiss: "task_suggestion_dismissed", stop: "task_stopped",
  resolve: "task_resolved", reopen: "task_reopened", retry: "task_retried",
};
/** Actions that are an answer to the open question (the answer is the apply; no /apply route). */
const ANSWERS = new Set<TaskAction>(["approve", "send", "publish", "merge"]);

export type ActionResult = { ok: true } | { ok: false; message: string };

export function errorCopy(error: unknown, answererNames: readonly string[] = []): string {
  if (error instanceof ApiError) {
    if (error.status === 409) return "Someone else got to this first. It's up to date now.";
    if (error.status === 403) return answererNames.length ? `Only ${answererNames.join(" or ")} can approve this` : "You can't do this here.";
    if (error.status === 404) return "This thread isn't available.";
    if (error.status === 503) return "Lightbulb is busy. Try again in a moment.";
  }
  return "That didn't go through. Try again.";
}

export function useTaskActions(source: string) {
  const { tasks } = useLiveEvents();
  const { cache, mutate } = useSWRConfig();

  const refetch = useCallback(async (taskId: string) => {
    const lists = [...cache.keys()].filter((k) => k.startsWith(`tasks:${tasks.cacheScope}:`));
    await Promise.all([mutate(taskKey(taskId)), ...lists.map((k) => mutate(k))]);
  }, [cache, mutate, tasks]);

  const act = useCallback(async (task: TaskSummary, verb: TaskVerb): Promise<ActionResult> => {
    try {
      await putTaskSummary(cache, mutate, await tasks.act(task.taskId, verb, task.version), tasks);
      capture(VERB_EVENTS[verb], { taskId: task.taskId, source, status: task.status });
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) await refetch(task.taskId);
      return { ok: false, message: errorCopy(error) };
    }
  }, [tasks, cache, mutate, refetch, source]);

  /** Answer the open question with one option. `expectVersion` defaults to the question's current version. */
  const answer = useCallback(async (
    task: TaskSummary, qid: string, optionId: string, opts: { text?: string; expectVersion?: number } = {},
  ): Promise<ActionResult & { answeredBy?: string }> => {
    try {
      const expectVersion = opts.expectVersion
        ?? (await tasks.questions(task.taskId)).items.find((q) => q.id === qid)?.version ?? task.version;
      const res = await tasks.answer(task.taskId, qid, {
        optionId, ...(opts.text ? { text: opts.text } : {}), clientAnswerId: crypto.randomUUID(), expectVersion,
      });
      if (res.next) await putTaskSummary(cache, mutate, res.next, tasks);
      else await refetch(task.taskId);
      capture("task_question_answered", { taskId: task.taskId, source, status: task.status });
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await refetch(task.taskId);
        const by = (await tasks.questions(task.taskId).catch(() => null))?.items.find((q) => q.id === qid)?.answeredBy?.name;
        return { ok: false, message: by ? `Already answered by ${by}` : errorCopy(error), answeredBy: by };
      }
      return { ok: false, message: errorCopy(error, task.openQuestion?.answererNames) };
    }
  }, [tasks, cache, mutate, refetch, source]);

  /** The card/row primary action. `answer` (free text) has no one-press form: it opens the thread. */
  const run = useCallback((task: TaskSummary, action: TaskAction): Promise<ActionResult> => {
    if (ANSWERS.has(action) && task.openQuestion) return answer(task, task.openQuestion.qid, action);
    if (action === "start" || action === "retry" || action === "reopen") return act(task, action);
    return Promise.resolve({ ok: true });
  }, [act, answer]);

  return { act, answer, run, refetch };
}

export const ACTION_LABELS: Record<TaskAction, string> = {
  start: "Start", approve: "Approve", answer: "Answer", review: "Review", retry: "Retry", reopen: "Reopen",
  publish: "Publish", send: "Send", merge: "Merge",
};
