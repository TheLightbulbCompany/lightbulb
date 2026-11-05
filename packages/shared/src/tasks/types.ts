// API shapes for the super-agent task routes (contracts-v2 §4–§6, wave2-README C15).
// `TaskSummary` mirrors `views.TaskSummary` in `fixtures/super-agent/tasks-schema.json`
// (built by `tasks_access.summarize`); `summary.test.ts` fails if a field drifts.

import type { ShownGroup, TaskStatus } from "./view";

export const OUTPUT_KINDS = ["document", "file", "app", "pr", "mail", "link", "capability"] as const;
export type OutputKind = (typeof OUTPUT_KINDS)[number];
export type QuestionKind = "question" | "approval" | "review";

export interface QuestionOption { id: string; label: string }

/** The open question this viewer sees on the task (theirs first, else the first open one). */
export interface SummaryQuestion {
  qid: string;
  kind: QuestionKind;
  subkind?: string | null;
  title: string;
  answerers: string[];
  answererNames: string[];
  canAnswer: boolean;
  options?: QuestionOption[];
}

export interface TaskSummary {
  taskId: string;
  identifier: string;
  title: string;
  status: TaskStatus;
  /** "stopped" on a resolved task a person stopped. */
  statusReason?: string | null;
  /** Viewer-specific, computed by the server; null = proposed (chat's Suggested block). Never re-derived here. */
  shownGroup: ShownGroup | null;
  /** Specialist handle ("finance"); shown as "Lightbulb · Finance". */
  specialist?: string | null;
  ownerUserId?: string | null;
  audience: { kind: "person" | "room" | "workspace"; userIds?: string[]; roomId?: string | null };
  audienceKind: "person" | "room" | "workspace";
  originRoomId?: string | null;
  taskRoomId: string;
  proposalReason?: string | null;
  openQuestion?: SummaryQuestion | null;
  /** The effect being applied (Landing), else null. */
  landing?: string | null;
  output?: { idemKey: string; kind: OutputKind; title: string; nextStep?: string | null } | null;
  watchUntil?: string | null;
  watching: boolean;
  runsOn?: "task" | "workspace" | null;
  activity?: { n: number; status: "dispatched" | "running" | "ended"; outcome?: string | null; lastHeartbeatAt?: string | null } | null;
  recentInputs?: string[];
  attemptCount?: number;
  usage?: { inputTokens?: number; outputTokens?: number; costUsd?: number } | null;
  costUsd?: number;
  budgetUsd?: number | null;
  updatedAt: string;
  version: number;
}

/** `routers/questions.py` `_view`: one Question as one viewer sees it. */
export interface QuestionView {
  id: string;
  taskId: string;
  kind: QuestionKind;
  subkind: string | null;
  title: string;
  text: string;
  preview?: unknown;
  options: (QuestionOption & { style: "primary" | "secondary" | "danger" })[];
  state: "open" | "answered" | "cancelled";
  answerers: { id: string; name: string }[];
  canAnswer: boolean;
  waitingLabel: string | null;
  answer: { optionId?: string | null; text?: string | null } | null;
  answeredBy: { id: string; name: string } | null;
  answeredAt: string | null;
  createdAt: string;
  version: number;
}

export interface TaskAttempt {
  n: number;
  status: "dispatched" | "running" | "ended";
  outcome?: string | null;
  startedAt: string;
  endedAt?: string | null;
  usage?: { costUsd?: number } | null;
}

export interface TaskEffect { idemKey: string; tool: string; state: string; output?: { kind: OutputKind; title: string } | null }
export interface TaskSubscription { threadKey: string; createdBy: "router" | "person" | "schedule"; active?: boolean }

/** `GET /api/v1/tasks/{id}`. */
export interface TaskDetail {
  task: TaskSummary;
  attempts: TaskAttempt[];
  questions: QuestionView[];
  effects: TaskEffect[];
  subscriptions: TaskSubscription[];
  taskRoomId: string;
}

export type TaskScope = { kind: "room"; roomId: string } | { kind: "mine" } | { kind: "workspace" };
export type TaskVerb = "start" | "dismiss" | "stop" | "resolve" | "reopen" | "retry";

export interface AnswerBody {
  optionId: string | null;
  text?: string;
  edits?: unknown;
  clientAnswerId: string;
  /** The question's version the answerer saw; omitted when unknown (the server still refuses a closed question). */
  expectVersion?: number;
}

/** Message attrs Lightbulb sets on task messages (contracts-v2 §6; absent on every other message). */
export interface TaskRef { taskId: string; role: "started" | "suggested" }
