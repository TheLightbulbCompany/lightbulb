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
  /** A money ask's currency, when the summary carries one; absent, an amount is shown without a symbol. */
  currency?: string | null;
  answerers: string[];
  answererNames: string[];
  canAnswer: boolean;
  options?: QuestionOption[];
}

/** What a task made (`tasks_schema.TaskOutput`). `ref` by kind: app `{appId}`; document/file `{roomId, attachmentId,
 * fileName, contentType}` (open with `GET /messaging/rooms/{roomId}/attachments/{attachmentId}/url`); mail
 * `{idemKey, to, subject, resultRef}`; pr `{idemKey, repo, number, url, resultRef}`. */
export interface TaskOutput {
  id: string;
  kind: OutputKind;
  title: string;
  ref: Record<string, string>;
  createdAt: string;
}

/** `GET /tasks/outputs` (Creations): one output with the task that made it. */
export interface CreationOutput extends TaskOutput {
  taskId: string;
  identifier: string;
  taskTitle: string;
  taskStatus: TaskStatus;
  specialist: string | null;
  roomId: string | null;
}

export interface TaskSummary {
  taskId: string;
  identifier: string;
  title: string;
  status: TaskStatus;
  /** "stopped" on a resolved task a person stopped. */
  statusReason?: string | null;
  /** Viewer-specific, computed by the server. Never re-derived here. */
  shownGroup: ShownGroup | null;
  /** Specialist handle ("finance") or agent id; shown by its name ("Finance"). */
  specialist?: string | null;
  ownerUserId?: string | null;
  audience: { kind: "person" | "room" | "workspace"; userIds?: string[]; roomId?: string | null };
  audienceKind: "person" | "room" | "workspace";
  originRoomId?: string | null;
  taskRoomId: string;
  openQuestion?: SummaryQuestion | null;
  /** The effect being applied (Landing), else null. */
  landing?: string | null;
  /** The draft waiting on a review, else null. */
  output?: { idemKey: string; kind: OutputKind; title: string; nextStep?: string | null } | null;
  /** What the task made, for every status, oldest first. */
  outputs?: TaskOutput[];
  watchUntil?: string | null;
  watching: boolean;
  runsOn?: "task" | "workspace" | null;
  activity?: {
    n: number; status: "dispatched" | "running" | "ended"; outcome?: string | null; lastHeartbeatAt?: string | null;
    /** The latest run's start and end: the chip's timer ("Working 0:38", "Done · 1:02"). */
    startedAt?: string | null; endedAt?: string | null;
  } | null;
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

/** An Effect row (`GET /tasks/{id}`). `resultRef` starting `refused:` on a `done` row means it did NOT happen (lanes/Q.md). */
export interface TaskEffect {
  idemKey: string; tool: string; kind?: string; state: string;
  output?: { kind: OutputKind; title: string } | null; resultRef?: string | null;
}
export interface TaskSubscription { threadKey: string; createdBy: "router" | "person" | "schedule"; active?: boolean }

/** A Question row as `GET /tasks/{id}` returns it (schema `question`; not viewer-specific — see `QuestionView`). */
export interface TaskQuestionRow {
  qid: string;
  kind: QuestionKind;
  subkind?: string | null;
  title: string;
  text?: string;
  options?: QuestionOption[];
  answerers: string[];
  state: "open" | "answered" | "cancelled";
  answer?: { optionId?: string | null; text?: string | null } | null;
  answeredBy?: string | null;
  answeredAt?: string | null;
  createdAt: string;
  version: number;
}

/** `GET /tasks/{id}/trace?attempt=n`: the Attempt's live tool list from the bridge. */
export interface TaskTrace { attempt: number; recentTools: unknown[] }

/** `GET /api/v1/tasks/{id}`. Viewer-specific question views come from `GET /tasks/{id}/questions`. */
export interface TaskDetail {
  task: TaskSummary;
  attempts: TaskAttempt[];
  questions: TaskQuestionRow[];
  effects: TaskEffect[];
  subscriptions: TaskSubscription[];
  taskRoomId: string;
}

export type TaskScope = { kind: "room"; roomId: string } | { kind: "mine" } | { kind: "workspace" };
export type TaskVerb = "stop" | "resolve" | "reopen" | "retry";

export interface AnswerBody {
  optionId: string | null;
  text?: string;
  edits?: unknown;
  clientAnswerId: string;
  /** The question's version the answerer saw; omitted when unknown (the server still refuses a closed question). */
  expectVersion?: number;
}

/** Message attrs Lightbulb sets on task messages (contracts-v2 §6; absent on every other message). */
export interface TaskRef { taskId: string; role: "started"; qid?: string }

/** Lane A's `GET /api/v1/automations` row (`routers/automations.py _view`, #4754): `nextRuns` are UTC ISO strings. */
export interface AutomationView {
  id: string;
  name: string;
  instructions: string;
  status: "draft" | "active" | "paused" | "paused_system";
  pausedReason: string | null;
  schedule: { cron: string[]; tz: string; remainingRuns?: number | null } | null;
  nextRuns: string[];
  hooks: { publicId: string; provider: string; events: string[]; fireUrl: string; armed: boolean; lastSigFailAt: string | null }[];
  specialistId: string | null;
  /** A room id, a DM target, or "none". */
  deliverTo: string;
  budgetPerRunUsd: number | null;
  ownerUserId: string;
  createdBy: "user" | "router";
  mode: "off" | "observe" | "auto" | null;
  lastTaskId: string | null;
  activeRunTaskId: string | null;
  runCount: number;
  /** Set on a proactive rule (lane S): matching sources/rooms. A rule has no Run now. */
  match?: { sources?: string[]; roomIds?: string[] } | null;
  version: number;
}
