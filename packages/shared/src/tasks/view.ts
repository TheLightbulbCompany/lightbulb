// Task row → the one status word, overview group and primary action every client shows
// (contracts-v2 §4). A task is a "request" to people, and it reads as one of four words:
// Working, Needs you, Done, Closed (founder ruling, 5 Nov 2026); `reason` is the short line under it. Pure; types mirror `fixtures/super-agent/tasks-schema.json` and
// `view.test.ts` fails if the schema's enums drift.

import type { TaskEffect, TaskSummary } from "./types";

export const TASK_STATUSES = ["queued", "working", "waiting", "idle", "resolved", "failed"] as const;
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

export type TaskAction = "approve" | "answer" | "review" | "retry" | "reopen" | "publish" | "send" | "merge";

/** The only four statuses people see. */
export const STATUS_WORDS = ["Working", "Needs you", "Done", "Closed"] as const;
export type StatusWord = (typeof STATUS_WORDS)[number];

export interface TaskView {
  word: StatusWord;
  /** Why, in a few words, where the UI has room ("Ready for review", "Waiting on Sam"); null when the word says it all. */
  reason: string | null;
  /** null = not in the overview. */
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

const view = (word: StatusWord, group: ShownGroup | null, action: TaskAction | null = null, reason: string | null = null): TaskView =>
  ({ word, reason, group, action });

export function taskView({ task, viewerId, questions, effects, nameOf }: TaskViewInput): TaskView {
  switch (task.status) {
    case "resolved": return view("Closed", "resolved", "reopen");
    // Failed is the owner's to retry; for anyone else it ended.
    case "failed": return task.ownerUserId === viewerId
      ? view("Needs you", "waiting_on_you", "retry", "Failed")
      : view("Closed", "idle", null, "Failed");
    // Idle with a watch armed is still going: it wakes itself when the watched thing finishes (watches.py).
    case "idle": return task.watchUntil ? view("Working", "idle", null, "Checking back later") : view("Done", "idle");
  }

  // queued / working / waiting
  const open = questions.filter((q) => q.taskId === task.taskId && q.state === "open");
  const mine = open.filter((q) => q.answerers.includes(viewerId));
  const ask = mine.find((q) => q.kind !== "review");
  if (ask) return ask.kind === "approval"
    ? view("Needs you", "waiting_on_you", "approve", "Needs approval")
    : view("Needs you", "waiting_on_you", "answer", "Needs an answer");
  if (mine.length) return view("Needs you", "ready_for_review", "review", "Ready for review");
  if (effects.some((e) => e.taskId === task.taskId && (e.state === "approved" || e.state === "intent"))) {
    return view("Working", "landing", null, "Applying");
  }
  const others = [...new Set(open.flatMap((q) => q.answerers))];
  if (others.length) return view("Working", "working", null, `Waiting on ${waitingOn(others, nameOf)}`);
  return view("Working", "working", null, LIVE_REASONS[task.status]);
}

const LIVE_REASONS: Record<string, string | null> = { queued: "Starting", working: null, waiting: "Waiting on a reply" };

/** Any one answerer can answer: "Sam", "Sam or Alex", "Sam or 2 others". */
function waitingOn([first, ...rest]: string[], nameOf: (userId: string) => string): string {
  if (!rest.length) return nameOf(first);
  return rest.length === 1 ? `${nameOf(first)} or ${nameOf(rest[0])}` : `${nameOf(first)} or ${rest.length} others`;
}

// ---------------------------------------------------------------------------------------------
// Rendering a server `TaskSummary` (clients never re-derive `shownGroup`; contracts-v2 §4).

export type TaskTone = "working" | "waiting" | "review" | "landing" | "neutral";
export interface SummaryView extends TaskView { tone: TaskTone }

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

/** A handle that reads as a name ("finance", "customer success"); anything with digits, underscores or other marks
 *  is an id ("agent_7f3", a uuid) and never shown. */
const HANDLE = /^[a-z]+(?:[ -][a-z]+)*$/i;

/** The specialist as the actor, by its own name (each is its own profile; founder, 5 Nov): "finance" → "Finance";
 *  an agent id → its name from `names`; no specialist, or an id not (yet) in the list → "Lightbulb" (an id is never
 *  shown as a name: only a letters-only handle is). */
export function specialistLabel(specialist: string | null | undefined, names?: AgentNames): string {
  if (!specialist) return "Lightbulb";
  const named = names?.get(specialist);
  if (named) return named;
  if (!HANDLE.test(specialist)) return "Lightbulb";
  return `${specialist.charAt(0).toUpperCase()}${specialist.slice(1)}`;
}

/** A message posted for a specialist names it by id in its text ("**22cc1ca5-…** has a draft ready"): read the id
 *  as the specialist's name, like the byline. A handle ("finance") already reads as a name and is left alone. */
export function nameSpecialistIn(text: string, specialist: string | null | undefined, names?: AgentNames): string {
  if (!specialist || HANDLE.test(specialist) || !text.includes(specialist)) return text;
  return text.split(specialist).join(specialistLabel(specialist, names));
}

function toneOf(word: StatusWord, group: ShownGroup | null): TaskTone {
  if (word === "Working") return group === "landing" ? "landing" : "working";
  if (word === "Needs you") return group === "ready_for_review" ? "review" : "waiting";
  return "neutral";
}

/** Word, reason, tone and the one action for a summary, from the server's `shownGroup`. */
export function summaryView(t: TaskSummary, viewerId: string): SummaryView {
  const group = t.shownGroup;
  const v = (word: StatusWord, reason: string | null = null, action: TaskAction | null = null): SummaryView =>
    ({ word, reason, group, action, tone: toneOf(word, group) });
  const q = t.openQuestion;
  // Dismissed = a suggestion from before 5 Nov 2026 (proposals removed; the server reads it as resolved): it never
  // ran, so no Reopen on its card.
  if (t.status === "resolved" && t.statusReason === "dismissed") return v("Closed", "Dismissed");
  if (t.status === "resolved") return v("Closed", t.statusReason === "stopped" ? "Stopped" : null, "reopen");
  // The owner's failed request needs them (retry); anyone else's is over (the server files it under idle).
  if (t.status === "failed") return t.ownerUserId === viewerId ? v("Needs you", "Failed", "retry") : v("Closed", "Failed");
  // One-press Approve only for an effect approval (allow_once/deny, questions.py). A worker's own approval ask can
  // carry any option ids, or none: its card opens the request instead.
  if (group === "waiting_on_you") {
    return q?.kind === "approval"
      ? v("Needs you", "Needs approval", q.options?.some((o) => o.id === "allow_once") ? "approve" : "answer")
      : v("Needs you", "Needs an answer", "answer");
  }
  if (group === "ready_for_review") {
    const next = (t.output?.nextStep ?? "review") as TaskAction;
    return v("Needs you", "Ready for review", NEXT_STEPS.has(next) ? next : "review");
  }
  if (group === "landing") return v("Working", "Applying");
  // Watching = idle with a watch armed: it wakes itself when the watched thing finishes (watches.py), so it is
  // still Working, not Done.
  if (t.status === "idle") return t.watching ? v("Working", "Checking back later") : v("Done");
  if (q && !q.canAnswer && q.answererNames.length) return v("Working", `Waiting on ${joinNames(q.answererNames)}`);
  // Waiting with no named answerer in the summary: say what it waits for, never a bare "Waiting".
  // ponytail: names the person once the summary carries who (today openQuestion is absent here).
  return v("Working", LIVE_REASONS[t.status] ?? null);
}

/** The request pane's actor line, from the same view as the status word: "Finance is working on this" while it
 *  runs, "Done by Finance, ready for your review" / "Finance needs you" when it waits on the viewer, "Done by
 *  Finance" once it is done. Null where nobody did it: a request closed with a reason (stopped, dismissed,
 *  failed): the status line already says why. With no specialist Lightbulb is the actor. */
export function doneByLine(specialist: string | null | undefined, view: Pick<TaskView, "word" | "reason">, names?: AgentNames): string | null {
  if (view.word === "Closed" && view.reason) return null;
  const who = specialistLabel(specialist, names);
  if (view.word === "Needs you") return view.reason === "Ready for review" ? `Done by ${who}, ready for your review` : `${who} needs you`;
  return view.word === "Done" || view.word === "Closed" ? `Done by ${who}` : `${who} is working on this`;
}

function joinNames([first, ...rest]: string[]): string {
  if (!rest.length) return first;
  return rest.length === 1 ? `${first} or ${rest[0]}` : `${first} or ${rest.length} others`;
}

/** Overview headings: the four status words, in this order. */
export type OverviewGroupId = "needs_you" | "working" | "done" | "closed";
export const GROUP_ORDER: readonly OverviewGroupId[] = ["needs_you", "working", "done", "closed"];
export const GROUP_LABELS: Record<OverviewGroupId, StatusWord> = {
  needs_you: "Needs you", working: "Working", done: "Done", closed: "Closed",
};

export interface OverviewGroup {
  id: OverviewGroupId; label: StatusWord; tasks: TaskSummary[]; alwaysShown: boolean; collapsed: boolean;
}

/** The heading a summary sits under; matches `summaryView`'s word without needing the viewer (the server's
 *  `shownGroup` already is per viewer). */
function overviewGroupOf(t: TaskSummary): OverviewGroupId | null {
  switch (t.shownGroup) {
    case "waiting_on_you": case "ready_for_review": return "needs_you";
    case "working": case "landing": return "working";
    case "resolved": return "closed";
    // The server's idle group also holds watched and (someone else's) failed requests.
    case "idle": return t.status === "failed" ? "closed" : t.watching ? "working" : "done";
    default: return null;
  }
}

/** Needs you and Closed always (Closed collapsed); the rest only when non-empty. */
export function overviewGroups(ts: readonly TaskSummary[]): OverviewGroup[] {
  return GROUP_ORDER.map((id): OverviewGroup => ({
    id, label: GROUP_LABELS[id], tasks: ts.filter((t) => overviewGroupOf(t) === id),
    alwaysShown: id === "needs_you" || id === "closed", collapsed: id === "closed",
  })).filter((g) => g.alwaysShown || g.tasks.length > 0);
}

/** One line under a row's title: what it needs, or why it reads its word. The specialist is its own line. */
export function taskSummaryLine(t: TaskSummary, viewerId: string): string {
  const q = t.openQuestion;
  const view = summaryView(t, viewerId);
  if (q && q.canAnswer) {
    if (q.kind === "approval") return `Needs approval: ${q.title}`;
    if (q.kind === "review") return `Ready for review: ${t.output?.title ?? q.title}`;
    return `Needs an answer: ${q.title}`;
  }
  if (view.group === "landing") return `Applying: ${t.output?.title ?? "changes"}`;
  return view.reason ?? view.word;
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

/** What the approver is asked: "Allow Engineering to send 'Q3' to a@b.c" (the title names the action and
 *  its target, questions.py `title_for`). Other kinds read their title as written. */
export function askLine(q: { kind: string; title: string; text?: string | null; preview?: unknown }, specialist: string | null | undefined, names?: AgentNames): string {
  // Only an effect-born approval (it carries a preview; its title is questions.py `title_for`'s "send … to …")
  // reads as "Allow …"; a worker's own ask keeps its words.
  if (q.kind !== "approval" || !q.preview) return q.text || q.title;
  const title = q.title.charAt(0).toLowerCase() + q.title.slice(1);
  return `Allow ${specialistLabel(specialist, names)} to ${title}`;
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
 * Anything else (a day-of-month without a month, steps, …) is "Custom schedule", never the raw cron. Several entries join with " and ".
 * ponytail: the conversion uses the next occurrence from `now`; a zone pair whose DST flips between now and
 * then can read an hour off until that day.
 */
export function formatSchedule(cron: readonly string[], tz: string, viewerTz: string, now = Date.now()): string {
  const parts = cron.map((entry): string | null => {
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
    if (dom !== "*" || mon !== "*" || !words || !/^\d+$/.test(min) || !/^\d+$/.test(hour)) return null;
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
  });
  return parts.length && parts.every(Boolean) ? parts.join(" and ") : CUSTOM_SCHEDULE;
}

export const CUSTOM_SCHEDULE = "Custom schedule";
