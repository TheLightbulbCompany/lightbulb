// Every button that changes a task (the web twin: components/tasks/useTaskActions.ts): one POST, the
// returned summary written into the cache, a 409 answered by a refetch and never a second POST.

import { useCallback } from "react";
import { ApiError, primaryOptionId, putTaskSummary, questionsKey, taskKey, useTaskCache, type TaskAction, type TaskSummary, type TaskVerb } from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";

import { useLiveEvents } from "../../providers/live-events";

/** Actions that are an answer to the open question (the answer is the apply; no /apply route). */
const ANSWERS = new Set<TaskAction>(["approve", "send", "publish", "merge"]);

export type ActionResult = { ok: true } | { ok: false; message: string };

export function errorCopy(error: unknown, answererNames: readonly string[] = []): string {
  if (error instanceof ApiError) {
    if (error.status === 402 || error.status === 429) return error.message;
    if (error.status === 409) return "Someone else got to this first. It's up to date now.";
    if (error.status === 403) return answererNames.length ? `Only ${answererNames.join(" or ")} can approve this` : "You can't do this here.";
    if (error.status === 404) return "This request isn't available.";
    if (error.status === 503) return "Lightbulb is busy. Try again in a moment.";
  }
  return "That didn't go through. Try again.";
}

export function useTaskActions() {
  const { tasks } = useLiveEvents();
  const { cache, mutate } = useTaskCache();

  // One task, not every list: its detail and questions re-read, and the fresh summary written into the lists.
  const refetch = useCallback(async (taskId: string) => {
    const detail = await tasks.get(taskId).catch(() => null);
    await mutate(questionsKey(taskId));
    if (!detail) return;
    await mutate(taskKey(taskId), detail, { revalidate: false });
    await putTaskSummary(cache, mutate, detail.task, tasks);
  }, [cache, mutate, tasks]);

  const act = useCallback(async (task: TaskSummary, verb: TaskVerb): Promise<ActionResult> => {
    try {
      await putTaskSummary(cache, mutate, await tasks.act(task.taskId, verb, task.version), tasks);
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) await refetch(task.taskId);
      return { ok: false, message: errorCopy(error) };
    }
  }, [tasks, cache, mutate, refetch]);

  const answer = useCallback(async (
    task: TaskSummary, qid: string, optionId: string, opts: { text?: string; expectVersion?: number } = {},
  ): Promise<ActionResult & { answeredBy?: string }> => {
    try {
      const expectVersion = opts.expectVersion
        ?? (await tasks.questions(task.taskId).catch(() => null))?.items.find((q) => q.id === qid)?.version;
      const res = await tasks.answer(task.taskId, qid, {
        optionId, ...(opts.text ? { text: opts.text } : {}), clientAnswerId: randomUUID(),
        ...(expectVersion !== undefined ? { expectVersion } : {}),
      });
      if (res.next) await putTaskSummary(cache, mutate, res.next, tasks);
      else await refetch(task.taskId);
      return { ok: true };
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        await refetch(task.taskId);
        const by = (await tasks.questions(task.taskId).catch(() => null))?.items.find((q) => q.id === qid)?.answeredBy?.name;
        return { ok: false, message: by ? `Already answered by ${by}` : errorCopy(error), answeredBy: by };
      }
      return { ok: false, message: errorCopy(error, task.openQuestion?.answererNames) };
    }
  }, [tasks, cache, mutate, refetch]);

  const run = useCallback((task: TaskSummary, action: TaskAction): Promise<ActionResult> => {
    // An answer posts the question's own primary option id (allow_once, send, …), never the action word.
    if (ANSWERS.has(action) && task.openQuestion) {
      const optionId = primaryOptionId(task.openQuestion.options);
      if (!optionId) return Promise.resolve({ ok: false, message: "Open the request to answer this." });
      return answer(task, task.openQuestion.qid, optionId);
    }
    if (action === "retry" || action === "reopen") return act(task, action);
    return Promise.resolve({ ok: true });
  }, [act, answer]);

  return { act, answer, run, refetch };
}

export const ACTION_LABELS: Record<TaskAction, string> = {
  approve: "Approve", answer: "Answer", review: "Review", retry: "Retry", reopen: "Reopen",
  publish: "Publish", send: "Send", merge: "Merge",
};
