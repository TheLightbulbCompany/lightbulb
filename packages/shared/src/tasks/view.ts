// Task row → the one status word, overview group and primary action every client shows
// (contracts-v2 §4). Pure; types mirror `fixtures/super-agent/tasks-schema.json` and
// `view.test.ts` fails if the schema's enums drift.

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

export type TaskAction = "start" | "approve" | "answer" | "review" | "retry" | "reopen";

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
