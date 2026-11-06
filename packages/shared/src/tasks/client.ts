// The task routes (wave2-README C15). No `/outputs/.../apply`: a review answer with
// `optionId ∈ {publish, send, merge, approve}` is the apply.

import type { ApiMethods } from "../api/client";
import type { AnswerBody, CreationOutput, OutputKind, QuestionView, TaskDetail, TaskScope, TaskSummary, TaskTrace, TaskVerb } from "./types";

/** `GET /tasks/outputs` filters. `kind` repeats (Links = link + pr); `limit` counts tasks, not outputs. */
export interface OutputsFilter {
  kind?: OutputKind | readonly OutputKind[];
  roomId?: string;
  specialist?: string;
  /** Outputs whose title or file name holds it (⌘K). */
  q?: string;
  cursor?: string | null;
  limit?: number;
}

export const scopeParam = (scope: TaskScope): string =>
  scope.kind === "room" ? `room:${scope.roomId}` : scope.kind;

export interface TasksClient {
  /** The signed-in workspace (org, else user); part of every list cache key so a workspace switch never shows another tenant's threads. */
  cacheScope: string;
  list(scope: TaskScope, cursor?: string | null): Promise<{ tasks: TaskSummary[]; next: string | null }>;
  /** ⌘K: the first `limit` tasks the viewer can see whose title or identifier holds `q`. */
  searchTasks(q: string, limit: number, signal?: AbortSignal): Promise<{ tasks: TaskSummary[]; next: null }>;
  get(taskId: string): Promise<TaskDetail>;
  act(taskId: string, verb: TaskVerb, version: number): Promise<TaskSummary>;
  answer(taskId: string, qid: string, body: AnswerBody): Promise<{ question: QuestionView; next: TaskSummary | null }>;
  trace(taskId: string, attempt: number): Promise<TaskTrace>;
  markRead(taskId: string): Promise<unknown>;
  needsYou(): Promise<{ items: QuestionView[]; next: string | null }>;
  /** A task's questions, answered and cancelled included (`routers/questions.py`). */
  questions(taskId: string): Promise<{ items: QuestionView[] }>;
  /** What tasks made (Creations), newest first within a page. */
  outputs(filter?: OutputsFilter, signal?: AbortSignal): Promise<{ outputs: CreationOutput[]; next: string | null }>;
}

export function createTasksClient(api: ApiMethods, cacheScope = ""): TasksClient {
  const path = (taskId: string) => `/tasks/${encodeURIComponent(taskId)}`;
  return {
    cacheScope,
    list: (scope, cursor) => api.get(
      `/tasks?scope=${encodeURIComponent(scopeParam(scope))}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    ) as ReturnType<TasksClient["list"]>,
    searchTasks: (q, limit, signal) => api.get(
      `/tasks?${new URLSearchParams({ scope: "workspace", q, limit: String(limit) })}`,
      { signal },
    ) as ReturnType<TasksClient["searchTasks"]>,
    get: (taskId) => api.get(path(taskId)) as Promise<TaskDetail>,
    act: (taskId, verb, version) => api.post(`${path(taskId)}/${verb}`, { version }) as Promise<TaskSummary>,
    answer: (taskId, qid, body) =>
      api.post(`${path(taskId)}/questions/${encodeURIComponent(qid)}/answer`, body) as ReturnType<TasksClient["answer"]>,
    trace: (taskId, attempt) => api.get(`${path(taskId)}/trace?attempt=${attempt}`) as Promise<TaskTrace>,
    markRead: (taskId) => api.post(`${path(taskId)}/read`, {}),
    needsYou: () => api.get("/needs-you") as ReturnType<TasksClient["needsYou"]>,
    questions: (taskId) => api.get(`${path(taskId)}/questions`) as ReturnType<TasksClient["questions"]>,
    outputs: ({ kind, roomId, specialist, q: text, cursor, limit } = {}, signal) => {
      const q = new URLSearchParams();
      for (const k of kind === undefined ? [] : typeof kind === "string" ? [kind] : kind) q.append("kind", k);
      if (roomId) q.set("roomId", roomId);
      if (specialist) q.set("specialist", specialist);
      if (text) q.set("q", text);
      if (cursor) q.set("cursor", cursor);
      if (limit) q.set("limit", String(limit));
      const qs = q.toString();
      return api.get(`/tasks/outputs${qs ? `?${qs}` : ""}`, signal ? { signal } : undefined) as ReturnType<TasksClient["outputs"]>;
    },
  };
}
