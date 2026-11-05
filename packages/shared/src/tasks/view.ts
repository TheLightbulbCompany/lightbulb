// Task row → the one status word, overview group and primary action every client shows
// (contracts-v2 §4). Pure; types mirror `fixtures/super-agent/tasks-schema.json` and
// `view.test.ts` fails if the schema's enums drift.

import type { TaskEffect, TaskSummary } from "./types";

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
    // Idle with nothing left to watch = the work is finished: people read "Done", not a state name.
    case "idle": return view(task.watchUntil ? "Watching" : "Done", "idle");
  }

  // queued / working / waiting
  const open = questions.filter((q) => q.taskId === task.taskId && q.state === "open");
  const mine = open.filter((q) => q.answerers.includes(viewerId));
  const ask = mine.find((q) => q.kind !== "review");
  if (ask) return view("Waiting on you", "waiting_on_you", ask.kind === "approval" ? "approve" : "answer");
  if (mine.length) return view("Ready for review", "ready_for_review", "review");
  if (effects.some((e) => e.taskId === task.taskId && (e.state === "approved" || e.state === "intent"))) {
    return view("Applying", "landing");
  }
  const others = [...new Set(open.flatMap((q) => q.answerers))];
  if (others.length) return view(`Waiting on ${waitingOn(others, nameOf)}`, "working");
  return view(task.status === "queued" ? "Queued" : task.status === "working" ? "Working" : "Waiting on a reply", "working");
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

/** The front agent (`@lightbulb`): the backend-authored starter (or legacy seed) root CEO, by role, never by
 *  display name. Undefined until the roster answers or where no agent holds that role. Web and mobile share it
 *  so both pick the same agent. */
export function resolveCeoAgentId(
  agents: readonly { id: string; role?: string | null; reportsTo?: string | null; origin?: string | null }[],
): string | undefined {
  return agents.find((a) => a.role === "ceo" && !a.reportsTo && (a.origin === "Starter" || a.origin === "Seed"))?.id;
}

/** Agent id → display name (from the workspace's agents list; `useAgentDirectory`). */
export type AgentNames = ReadonlyMap<string, string>;

const UUID_LIKE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** "finance" → "Lightbulb · Finance"; an agent id → "Lightbulb · <its name>" from `names`; no specialist, or an id
 *  not (yet) in the list → "Lightbulb" (an id is never shown as a name). */
export function specialistLabel(specialist: string | null | undefined, names?: AgentNames): string {
  if (!specialist) return "Lightbulb";
  const named = names?.get(specialist);
  if (named) return `Lightbulb · ${named}`;
  if (UUID_LIKE.test(specialist)) return "Lightbulb";
  return `Lightbulb · ${specialist.charAt(0).toUpperCase()}${specialist.slice(1)}`;
}

/** Word, tone and the one action for a summary, from the server's `shownGroup`. */
export function summaryView(t: TaskSummary, viewerId: string): SummaryView {
  const group = t.shownGroup;
  const tone = group ? TONES[group] : "neutral";
  const v = (word: string, action: TaskAction | null = null): SummaryView => ({ word, group, action, tone });
  const q = t.openQuestion;
  if (t.status === "proposed") return v("Suggested", "start");
  // A dismissed suggestion never ran: no Reopen on its card.
  if (t.status === "resolved" && t.statusReason === "dismissed") return v("Dismissed");
  if (t.status === "resolved") return v(t.statusReason === "stopped" ? "Stopped" : "Resolved", "reopen");
  if (t.status === "failed") return v("Failed", t.ownerUserId === viewerId ? "retry" : null);
  // One-press Approve only for an effect approval (allow_once/deny, questions.py). A worker's own approval ask can
  // carry any option ids, or none: its card opens the thread instead.
  if (group === "waiting_on_you") return v("Waiting on you", q?.kind === "approval" && q.options?.some((o) => o.id === "allow_once") ? "approve" : "answer");
  if (group === "ready_for_review") {
    const next = (t.output?.nextStep ?? "review") as TaskAction;
    return v("Ready for review", NEXT_STEPS.has(next) ? next : "review");
  }
  if (group === "landing") return v("Applying");
  if (t.status === "idle") return v(t.watching ? "Watching" : "Done");
  if (q && !q.canAnswer && q.answererNames.length) return v(`Waiting on ${joinNames(q.answererNames)}`);
  // Waiting with no named answerer in the summary: say what it waits for, never a bare "Waiting".
  // ponytail: names the person once the summary carries who (today openQuestion is absent here).
  return v(t.status === "queued" ? "Queued" : t.status === "waiting" ? "Waiting on a reply" : "Working");
}

function joinNames([first, ...rest]: string[]): string {
  if (!rest.length) return first;
  return rest.length === 1 ? `${first} or ${rest[0]}` : `${first} or ${rest.length} others`;
}

export const GROUP_LABELS: Record<ShownGroup, string> = {
  waiting_on_you: "Waiting on you", working: "Working", ready_for_review: "Ready for review",
  landing: "Applying", idle: "Done", resolved: "Resolved",
};
/** Contract display order (mockup GROUPS). */
export const GROUP_ORDER: readonly ShownGroup[] = ["waiting_on_you", "working", "ready_for_review", "landing", "idle", "resolved"];

export interface OverviewGroup {
  /** A shown group, or one of the server's `idle` group's parts ("watching", "failed"). */
  id: ShownGroup | "watching" | "failed"; label: string; tasks: TaskSummary[]; alwaysShown: boolean; collapsed: boolean;
}

/** Waiting on you and Resolved always (Resolved collapsed); the rest only when non-empty. Proposed tasks are skipped. */
export function overviewGroups(ts: readonly TaskSummary[]): OverviewGroup[] {
  return GROUP_ORDER.flatMap((id): OverviewGroup[] => {
    const tasks = ts.filter((t) => t.shownGroup === id);
    const group = (gid: OverviewGroup["id"], label: string, rows: TaskSummary[]): OverviewGroup =>
      ({ id: gid, label, tasks: rows, alwaysShown: gid === "waiting_on_you" || gid === "resolved", collapsed: gid === "resolved" });
    // The server's `idle` group also holds watched and (someone else's) failed threads: each heading matches its rows.
    if (id === "idle") {
      return [
        group("idle", GROUP_LABELS.idle, tasks.filter((t) => t.status === "idle" && !t.watching)),
        group("watching", "Watching", tasks.filter((t) => t.status === "idle" && t.watching)),
        group("failed", "Failed", tasks.filter((t) => t.status === "failed")),
      ];
    }
    return [group(id, GROUP_LABELS[id], tasks)];
  }).filter((g) => g.alwaysShown || g.tasks.length > 0);
}

/** One line under a row's title. */
export function taskSummaryLine(t: TaskSummary, viewerId: string, names?: AgentNames): string {
  const q = t.openQuestion;
  const view = summaryView(t, viewerId);
  if (q && q.canAnswer) {
    if (q.kind === "approval") return `Needs approval: ${q.title}`;
    if (q.kind === "review") return `Ready for review: ${t.output?.title ?? q.title}`;
    return `Needs an answer: ${q.title}`;
  }
  if (view.group === "landing") return `Applying: ${t.output?.title ?? "changes"}`;
  if (t.status === "proposed") return t.proposalReason ?? "Suggested";
  if (view.group === "working") return `${view.word} · ${specialistLabel(t.specialist, names)}`;
  return view.word;
}

const EFFECT_NAMES: Record<string, string> = {
  "mail.send": "Email", "text.send": "Text message", "app.publish": "App publish", "pr.merge": "Pull request merge",
  purchase: "Purchase", provision: "Provider setup", "a2a.send": "Message to another agent",
};

/** What an effect is, in words: its output's title, else its kind ("Email"), never a raw tool id. */
export function effectName(effect: Pick<TaskEffect, "tool" | "kind" | "output">): string {
  return effect.output?.title ?? EFFECT_NAMES[effect.kind ?? effect.tool] ?? "An outside action";
}

// ---------------------------------------------------------------------------------------------
// Questions (option ids are the server's: questions.py `kind_and_options`, task_questions.py worker asks).

type OptionLike = { id: string; style?: string };

/** The one-press option: the one the server styles primary (routers/questions.py: the first option unless it is
 *  deny / request_changes), never a literal. Null when the question has no options (a free-text ask). */
export function primaryOptionId(options: readonly OptionLike[] | null | undefined): string | null {
  const o = options ?? [];
  return (o.find((x) => x.style === "primary") ?? o.find((x) => x.id !== "deny" && x.id !== "request_changes"))?.id ?? null;
}

/** A free-text question (a worker's ask with no options): answered with optionId "answer" + text (questions.py `answer`). */
export const FREE_TEXT_OPTION = "answer";

/** What the approver is asked: "Allow Lightbulb · Engineering to send 'Q3' to a@b.c" (the title names the action and
 *  its target, questions.py `title_for`). Other kinds read their title as written. */
export function askLine(q: { kind: string; title: string; text?: string | null; preview?: unknown }, specialist: string | null | undefined): string {
  // Only an effect-born approval (it carries a preview; its title is questions.py `title_for`'s "send … to …")
  // reads as "Allow …"; a worker's own ask keeps its words.
  if (q.kind !== "approval" || !q.preview) return q.text || q.title;
  const title = q.title.charAt(0).toLowerCase() + q.title.slice(1);
  return `Allow ${specialistLabel(specialist)} to ${title}`;
}

const PREVIEW_LABELS: Record<string, string> = {
  to: "To", subject: "Subject", body: "Message", text: "Message", amount: "Amount", item: "Item", provider: "Provider",
  appName: "App", repo: "Repository", number: "PR", target: "To", merchant: "Merchant", why: "Why", url: "Link", title: "Title",
};
/** The card's cut; the pane passes Infinity so a person reads the whole message before acting. */
const PREVIEW_MAX = 600;

/** The exact effect a person approves (`question.preview` = `{kind, ...operands}`, task_questions.py), as label/value
 *  rows. Known operands first in a fixed order; ids, hashes and nested values are left out. */
export function previewRows(preview: unknown, max = PREVIEW_MAX): { label: string; value: string }[] {
  if (!preview || typeof preview !== "object") return [];
  const p = preview as Record<string, unknown>;
  return Object.keys(PREVIEW_LABELS)
    .filter((k) => typeof p[k] === "string" || typeof p[k] === "number" || (Array.isArray(p[k]) && (p[k] as unknown[]).every((v) => typeof v === "string")))
    .filter((k, _i, keys) => !(k === "target" && keys.includes("to")) && !(k === "text" && keys.includes("body")))
    .map((k) => {
      const raw = Array.isArray(p[k]) ? (p[k] as unknown[]).join(", ") : String(p[k]);
      const value = k === "number" ? `#${raw}` : raw.length > max ? `${raw.slice(0, max - 1)}…` : raw;
      return { label: PREVIEW_LABELS[k], value };
    });
}

const ANSWER_VERBS: Record<string, string> = {
  allow_once: "Approved", approve: "Approved", deny: "Denied", send: "Sent", publish: "Published", merge: "Merged",
  request_changes: "Changes requested", answer: "Answered",
};

/** "Approved" / "Denied" / … for an answered question's option id; unknown ids read as their label. */
export function answerVerb(optionId: string | null | undefined, label?: string | null): string {
  return (optionId && ANSWER_VERBS[optionId]) || label || "Answered";
}

const EFFECT_WORDS: Record<string, string> = {
  proposed: "Waiting for approval", approved: "Approved", denied: "Denied", intent: "Applying", done: "Done",
  unknown: "Might have happened — check before retrying",
};

/** One line per external effect. A `done` row whose `resultRef` starts `refused:` did not happen (lanes/Q.md ruling, 5 Nov). */
export function effectLine(effect: Pick<TaskEffect, "state" | "resultRef">): string {
  const ref = effect.resultRef ?? "";
  if (ref.startsWith("refused:")) return `Didn't happen: ${ref.slice("refused:".length).replace(/_/g, " ") || "refused"}`;
  return EFFECT_WORDS[effect.state] ?? effect.state;
}

// ---------------------------------------------------------------------------------------------
// Schedules (spec 04 §4: plain words in the viewer's time zone; the row stores wall clock in its own zone).

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const DAY_NAMES: Record<string, number> = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };

/** Minutes a zone is ahead of UTC at `ts`, from Intl (no table of zones here). */
function zoneOffsetMinutes(ts: number, tz: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric",
  }).formatToParts(new Date(ts));
  const n = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  const asUtc = Date.UTC(n("year"), n("month") - 1, n("day"), n("hour"), n("minute"), n("second"));
  return Math.round((asUtc - Math.floor(ts / 1000) * 1000) / 60000);
}

/** The instant of `h:m` on `tz`'s next calendar day falling on weekday `dow` (today counts). */
function nextInstant(dow: number | null, h: number, m: number, tz: string, now: number): number {
  const today = new Intl.DateTimeFormat("en-US", { timeZone: tz, year: "numeric", month: "numeric", day: "numeric" }).formatToParts(new Date(now));
  const n = (type: string) => Number(today.find((p) => p.type === type)?.value);
  const base = Date.UTC(n("year"), n("month") - 1, n("day"), h, m);
  const todayDow = new Date(base).getUTCDay();
  const ahead = dow === null ? 0 : (dow - todayDow + 7) % 7;
  const wall = base + ahead * 86_400_000;
  return wall - zoneOffsetMinutes(wall, tz) * 60_000;
}

function dayWords(dow: string): { label: string; days: (number | null)[]; range?: [number, number] } | null {
  if (dow === "*") return { label: "Every day", days: [null] };
  const one = (token: string) => (/^\d$/.test(token) ? Number(token) % 7 : DAY_NAMES[token.toLowerCase().slice(0, 3)]);
  const range = dow.match(/^(\w+)-(\w+)$/);
  if (range) {
    const [a, b] = [one(range[1]), one(range[2])];
    if (a === undefined || b === undefined) return null;
    return { label: a === 1 && b === 5 ? "Weekdays" : `${DAYS[a]}s to ${DAYS[b]}s`, days: [a], range: [a, b] };
  }
  const days = dow.split(",").map(one);
  if (days.some((d) => d === undefined)) return null;
  if (days.length === 2 && days.includes(0) && days.includes(6)) return { label: "Weekends", days: [6, 0] };
  return { label: days.map((d) => `${DAYS[d]}s`).join(" and "), days };
}

/**
 * "Mondays 9:00" for `["0 9 * * 1"]` in `tz`, read in `viewerTz` ("Mondays 14:00" from New York to London).
 * A day-of-month or month field returns the raw entry. Several entries join with " and ".
 * ponytail: the conversion uses the next occurrence from `now`; a zone pair whose DST flips between now and
 * then can read an hour off until that day.
 */
export function formatSchedule(cron: readonly string[], tz: string, viewerTz: string, now = Date.now()): string {
  return cron.map((entry) => {
    const [min, hour, dom, mon, dow] = entry.trim().split(/\s+/);
    // A one-off ("27 10 5 11 *", what a "remind me at …" stores): "Once, 5 Nov, 10:27" in the viewer's zone.
    // ponytail: assumes this year in `tz`; a one-off set in December for January reads a year early.
    if (/^\d+$/.test(min) && /^\d+$/.test(hour) && /^\d+$/.test(dom) && /^\d+$/.test(mon) && (dow ?? "*") === "*") {
      const year = Number(new Intl.DateTimeFormat("en-US", { timeZone: tz, year: "numeric" }).format(new Date(now)));
      const wall = Date.UTC(year, Number(mon) - 1, Number(dom), Number(hour), Number(min));
      const instant = wall - zoneOffsetMinutes(wall, tz) * 60_000;
      return `Once, ${new Intl.DateTimeFormat("en-GB", { timeZone: viewerTz, day: "numeric", month: "short", hour: "numeric", minute: "2-digit", hourCycle: "h23" }).format(new Date(instant))}`;
    }
    const words = dayWords(dow ?? "*");
    if (dom !== "*" || mon !== "*" || !words || !/^\d+$/.test(min) || !/^\d+$/.test(hour)) return entry;
    const first = words.days[0];
    const instant = nextInstant(first, Number(hour), Number(min), tz, now);
    const time = new Intl.DateTimeFormat("en-GB", { timeZone: viewerTz, hourCycle: "h23", hour: "numeric", minute: "2-digit" }).format(new Date(instant));
    if (first === null) return `${words.label} ${time}`;
    // The viewer's zone can put the run on the day before or after the row's.
    const viewerDow = DAY_NAMES[new Intl.DateTimeFormat("en-US", { timeZone: viewerTz, weekday: "short" }).format(new Date(instant)).toLowerCase()];
    const shift = (viewerDow - first + 7) % 7 === 6 ? -1 : (viewerDow - first + 7) % 7;
    const day = (d: number) => `${DAYS[(d + shift + 7) % 7]}s`;
    const label = shift === 0 ? words.label
      : words.range ? `${day(words.range[0])} to ${day(words.range[1])}`
      : words.days.map((d) => day(d ?? 0)).join(" and ");
    return `${label} ${time}`;
  }).join(" and ");
}
