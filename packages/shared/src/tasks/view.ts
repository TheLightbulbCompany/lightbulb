// Task row → the one status word, overview group and primary action every client shows
// (contracts-v2 §4). Pure; types mirror `fixtures/super-agent/tasks-schema.json` and
// `view.test.ts` fails if the schema's enums drift.

import type { TaskSummary } from "./types";

export const TASK_STATUSES = ["proposed", "queued", "working", "waiting", "idle", "resolved", "failed"] as const;
export const SHOWN_GROUPS = ["waiting_on_you", "ready_for_review", "landing", "working", "idle", "resolved"] as const;
export const QUESTION_KINDS = ["question", "approval", "review"] as const;
export const QUESTION_STATES = ["open", "answered", "cancelled"] as const;
export const EFFECT_STATES = ["proposed", "approved", "denied", "intent", "done", "unknown"] as const;

export type TaskStatus = (typeof TASK_STATUSES)[number];
export type ShownGroup = (typeof SHOWN_GROUPS)[number];

/** The Task, Question and Effect row fields this needs (schema names). */
export interface TaskRow { taskId: string; status: TaskStatus; ownerUserId: string | null; watchUntil: string | null }
export interface QuestionRow {
  taskId: string; qid: string; kind: (typeof QUESTION_KINDS)[number];
  answerers: string[]; state: (typeof QUESTION_STATES)[number];
}
export interface EffectRow { taskId: string; idemKey: string; state: (typeof EFFECT_STATES)[number] }

export type TaskAction = "start" | "approve" | "answer" | "review" | "retry" | "reopen" | "publish" | "send" | "merge";

export interface TaskView {
  word: string;
  /** null = not in the overview (a proposed task lives in chat's Suggested block). */
  group: ShownGroup | null;
  action: TaskAction | null;
}

export interface TaskViewInput {
  task: TaskRow;
  viewerId: string;
  /** The task's Question rows (any state; others' tasks are ignored). */
  questions: readonly QuestionRow[];
  /** The task's Effect rows; approved/intent means `apply_effect` is running. */
  effects: readonly EffectRow[];
  nameOf: (userId: string) => string;
}

const view = (word: string, group: ShownGroup | null, action: TaskAction | null = null): TaskView =>
  ({ word, group, action });

export function taskView({ task, viewerId, questions, effects, nameOf }: TaskViewInput): TaskView {
  switch (task.status) {
    case "proposed": return view("Suggested", null, "start");
    case "resolved": return view("Resolved", "resolved", "reopen");
    case "failed": return task.ownerUserId === viewerId
      ? view("Failed", "waiting_on_you", "retry")
      : view("Failed", "idle");
    case "idle": return view(task.watchUntil ? "Watching" : "Idle", "idle");
  }

  // queued / working / waiting
  const open = questions.filter((q) => q.taskId === task.taskId && q.state === "open");
  const mine = open.filter((q) => q.answerers.includes(viewerId));
  const ask = mine.find((q) => q.kind !== "review");
  if (ask) return view("Waiting on you", "waiting_on_you", ask.kind === "approval" ? "approve" : "answer");
  if (mine.length) return view("Ready for review", "ready_for_review", "review");
  if (effects.some((e) => e.taskId === task.taskId && (e.state === "approved" || e.state === "intent"))) {
    return view("Landing", "landing");
  }
  const others = [...new Set(open.flatMap((q) => q.answerers))];
  if (others.length) return view(`Waiting on ${waitingOn(others, nameOf)}`, "working");
  return view(task.status === "queued" ? "Queued" : task.status === "working" ? "Working" : "Waiting", "working");
}

/** Any one answerer can answer: "Sam", "Sam or Alex", "Sam or 2 others". */
function waitingOn([first, ...rest]: string[], nameOf: (userId: string) => string): string {
  if (!rest.length) return nameOf(first);
  return rest.length === 1 ? `${nameOf(first)} or ${nameOf(rest[0])}` : `${nameOf(first)} or ${rest.length} others`;
}

// ---------------------------------------------------------------------------------------------
// Rendering a server `TaskSummary` (clients never re-derive `shownGroup`; contracts-v2 §4).

export type TaskTone = "working" | "waiting" | "review" | "landing" | "neutral";
export interface SummaryView extends TaskView { tone: TaskTone }

const TONES: Record<ShownGroup, TaskTone> = {
  waiting_on_you: "waiting", ready_for_review: "review", landing: "landing",
  working: "working", idle: "neutral", resolved: "neutral",
};
const NEXT_STEPS = new Set<TaskAction>(["publish", "send", "merge", "approve"]);

/** "finance" → "Lightbulb · Finance"; no specialist → "Lightbulb". */
export function specialistLabel(specialist: string | null | undefined): string {
  if (!specialist) return "Lightbulb";
  return `Lightbulb · ${specialist.charAt(0).toUpperCase()}${specialist.slice(1)}`;
}

/** Word, tone and the one action for a summary, from the server's `shownGroup`. */
export function summaryView(t: TaskSummary, viewerId: string): SummaryView {
  const group = t.shownGroup;
  const tone = group ? TONES[group] : "neutral";
  const v = (word: string, action: TaskAction | null = null): SummaryView => ({ word, group, action, tone });
  const q = t.openQuestion;
  if (t.status === "proposed") return v("Suggested", "start");
  if (t.status === "resolved") return v(t.statusReason === "stopped" ? "Stopped" : "Resolved", "reopen");
  if (t.status === "failed") return v("Failed", t.ownerUserId === viewerId ? "retry" : null);
  if (group === "waiting_on_you") return v("Waiting on you", q?.kind === "approval" ? "approve" : "answer");
  if (group === "ready_for_review") {
    const next = (t.output?.nextStep ?? "review") as TaskAction;
    return v("Ready for review", NEXT_STEPS.has(next) ? next : "review");
  }
  if (group === "landing") return v("Landing");
  if (t.status === "idle") return v(t.watching ? "Watching" : "Idle");
  if (q && !q.canAnswer && q.answererNames.length) return v(`Waiting on ${joinNames(q.answererNames)}`);
  return v(t.status === "queued" ? "Queued" : t.status === "waiting" ? "Waiting" : "Working");
}

function joinNames([first, ...rest]: string[]): string {
  if (!rest.length) return first;
  return rest.length === 1 ? `${first} or ${rest[0]}` : `${first} or ${rest.length} others`;
}

export const GROUP_LABELS: Record<ShownGroup, string> = {
  waiting_on_you: "Waiting on you", working: "Working", ready_for_review: "Ready for review",
  landing: "Landing", idle: "Idle", resolved: "Resolved",
};
/** Contract display order (mockup GROUPS). */
export const GROUP_ORDER: readonly ShownGroup[] = ["waiting_on_you", "working", "ready_for_review", "landing", "idle", "resolved"];

export interface OverviewGroup {
  id: ShownGroup; label: string; tasks: TaskSummary[]; alwaysShown: boolean; collapsed: boolean;
}

/** Waiting on you and Resolved always (Resolved collapsed); the rest only when non-empty. Proposed tasks are skipped. */
export function overviewGroups(ts: readonly TaskSummary[]): OverviewGroup[] {
  return GROUP_ORDER.map((id) => ({
    id,
    label: GROUP_LABELS[id],
    tasks: ts.filter((t) => t.shownGroup === id),
    alwaysShown: id === "waiting_on_you" || id === "resolved",
    collapsed: id === "resolved",
  })).filter((g) => g.alwaysShown || g.tasks.length > 0);
}

/** One line under a row's title. */
export function taskSummaryLine(t: TaskSummary, viewerId: string): string {
  const q = t.openQuestion;
  const view = summaryView(t, viewerId);
  if (q && q.canAnswer) {
    if (q.kind === "approval") return `Needs approval: ${q.title}`;
    if (q.kind === "review") return `Ready for review: ${t.output?.title ?? q.title}`;
    return `Needs an answer: ${q.title}`;
  }
  if (view.group === "landing") return `Applying: ${t.output?.title ?? "changes"}`;
  if (t.status === "proposed") return t.proposalReason ?? "Suggested";
  if (view.group === "working") return `${view.word} · ${specialistLabel(t.specialist)}`;
  return view.word;
}
