// The task routes (wave2-README C15). No `/outputs/.../apply`: a review answer with
// `optionId ∈ {publish, send, merge, approve}` is the apply.

import type { ApiMethods } from "../api/client";
import type { AnswerBody, QuestionView, TaskDetail, TaskScope, TaskSummary, TaskTrace, TaskVerb } from "./types";

export const scopeParam = (scope: TaskScope): string =>
  scope.kind === "room" ? `room:${scope.roomId}` : scope.kind;

export interface TasksClient {
  /** The signed-in workspace (org, else user); part of every list cache key so a workspace switch never shows another tenant's threads. */
  cacheScope: string;
  list(scope: TaskScope, cursor?: string | null): Promise<{ tasks: TaskSummary[]; next: string | null }>;
  get(taskId: string): Promise<TaskDetail>;
  act(taskId: string, verb: TaskVerb, version: number): Promise<TaskSummary>;
  /** `POST /tasks/start`: the summaries of the ones that started; an id absent from `tasks` did not. */
  startMany(taskIds: string[]): Promise<{ tasks: TaskSummary[] }>;
  answer(taskId: string, qid: string, body: AnswerBody): Promise<{ question: QuestionView; next: TaskSummary | null }>;
  trace(taskId: string, attempt: number): Promise<TaskTrace>;
  markRead(taskId: string): Promise<unknown>;
  needsYou(): Promise<{ items: QuestionView[]; next: string | null }>;
  /** A task's questions, answered and cancelled included (`routers/questions.py`). */
  questions(taskId: string): Promise<{ items: QuestionView[] }>;
}

export function createTasksClient(api: ApiMethods, cacheScope = ""): TasksClient {
  const path = (taskId: string) => `/tasks/${encodeURIComponent(taskId)}`;
  return {
    cacheScope,
    list: (scope, cursor) => api.get(
      `/tasks?scope=${encodeURIComponent(scopeParam(scope))}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    ) as ReturnType<TasksClient["list"]>,
    get: (taskId) => api.get(path(taskId)) as Promise<TaskDetail>,
    act: (taskId, verb, version) => api.post(`${path(taskId)}/${verb}`, { version }) as Promise<TaskSummary>,
    startMany: (taskIds) => api.post("/tasks/start", { taskIds }) as Promise<{ tasks: TaskSummary[] }>,
    answer: (taskId, qid, body) =>
      api.post(`${path(taskId)}/questions/${encodeURIComponent(qid)}/answer`, body) as ReturnType<TasksClient["answer"]>,
    trace: (taskId, attempt) => api.get(`${path(taskId)}/trace?attempt=${attempt}`) as Promise<TaskTrace>,
    markRead: (taskId) => api.post(`${path(taskId)}/read`, {}),
    needsYou: () => api.get("/needs-you") as ReturnType<TasksClient["needsYou"]>,
    questions: (taskId) => api.get(`${path(taskId)}/questions`) as ReturnType<TasksClient["questions"]>,
  };
}
