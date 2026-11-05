/**
 * The Activity page's arrangement, pure so it is tested on its own: "For you"
 * (one row per item, newest first, Slack's Activity tab) and the team feed's
 * dividers (new since you looked, then days).
 */
import { activityRowParts, approvalActivityId, interactionActivityId, isNeedsYou, type ActivityItem, type CanonicalRoomMessage } from "@lightbulb/shared";
import type { FailedRunGroup } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { AttentionItem } from "@/components/teams/shared/types";

/** `reply`: a message (mention, DM, thread reply). `decide` / `do`: needs you. */
export type ForYouVerb = "decide" | "reply" | "do";

export type ForYouSource =
  | { type: "activity"; item: ActivityItem }
  | { type: "attention"; item: AttentionItem }
  /** The Inbox's failed runs (`/inbox/runs`), identical failures grouped, minus the ones dismissed here. */
  | { type: "failed_runs"; group: FailedRunGroup };

export interface ForYouEntry {
  id: string;
  verb: ForYouVerb;
  sourceId: string;
  sourceName: string;
  sourceKind: "human" | "agent" | "system";
  /** What the item is (`approval`, `mention`, `failed_run`…): what the chips filter on. */
  kind: string;
  /** "in #general", "in a DM", "in Work"; null when there is no place to name. */
  where: string | null;
  /** The message or the request, shown on up to two lines. */
  text: string;
  /** ISO time the row shows and sorts by. */
  at: string;
  /** A message not opened yet (the room's read cursor is behind it). Only message rows carry a dot. */
  unread: boolean;
  source: ForYouSource;
}

const MESSAGE_KINDS: readonly string[] = ["mention", "dm", "thread_reply"];

/**
 * REPLY: open mentions, DMs and thread replies, read or not (the Unread toggle and the dot
 * say which). DECIDE: approvals and agents' questions. DO: assigned tasks and the rest of
 * "needs you" (session, browser and extension asks). Anything else (a reaction, a finished
 * run, a decided approval, a mention marked done) is not for you: null.
 */
export function activityVerb(item: ActivityItem): ForYouVerb | null {
  if (item.state !== "open") return null;
  if (MESSAGE_KINDS.includes(item.kind)) return "reply";
  if (item.kind === "paperclip" && item.subtype === "assigned_issue") return "do";
  // Failed runs come from the Inbox's own list (every failure, with Retry and Dismiss), not this one.
  if (item.kind === "paperclip" && item.subtype === "failed_run") return null;
  if (!isNeedsYou(item)) return null;
  if (item.subtype === "approval" || item.subtype === "interaction") return "decide";
  return "do";
}

/**
 * The Teams attention kinds For you lists. Approvals and agents' questions
 * (`issue_thread_interaction`) are normally Activity items too: those are kept from the
 * attention feed only when Activity does not carry them (see `forYouEntries`).
 */
const ATTENTION_VERBS: Partial<Record<AttentionItem["sourceKind"], ForYouVerb>> = {
  approval: "decide",
  issue_thread_interaction: "decide",
  decision: "decide",
  join_request: "do",
  review: "do",
  blocker_attention: "do",
  recovery_action: "do",
};

/** Where it happened, Slack-style: a message by its room's kind, the rest by where it lives. */
function activityWhere(item: ActivityItem): string | null {
  const room = item.summary.links.find((link) => link.kind === "room");
  if (item.roomKind === "dm") return "in a DM";
  if (item.roomKind) return room ? `in ${room.label}` : null;
  if (item.kind === "paperclip") return "in Work";
  if (item.kind === "session") return "in a session";
  // A task question names its thread.
  const task = item.kind === "task_question" ? item.summary.links.find((link) => link.kind === "task") : undefined;
  if (task) return `in ${task.label}`;
  return null;
}

/** A message's own words; an ask's sentence after its actor ("needs your approval · Pilot X"). */
function activityText(item: ActivityItem): string {
  const parts = activityRowParts(item);
  if (MESSAGE_KINDS.includes(item.kind)) return item.summary.preview?.text ?? parts.snippet ?? item.summary.title;
  if (!parts.lead) return item.summary.title;
  return [parts.rest === ":" ? null : parts.rest, parts.snippet].filter(Boolean).join(" · ") || item.summary.title;
}

function activityEntry(item: ActivityItem, verb: ForYouVerb): ForYouEntry {
  return {
    id: item.id,
    verb,
    sourceId: `${item.actor.kind}:${item.actor.id}`,
    sourceName: item.actor.name,
    sourceKind: item.actor.kind,
    kind: item.subtype ?? item.kind,
    where: activityWhere(item),
    text: activityText(item),
    at: item.createdAt,
    unread: verb === "reply" && !item.read,
    source: { type: "activity", item },
  };
}

function attentionEntry(item: AttentionItem, verb: ForYouVerb): ForYouEntry {
  const name = item.originAgentName ?? "Your team";
  return {
    id: `attention:${item.id}`,
    verb,
    sourceId: `agent-name:${name}`,
    sourceName: name,
    sourceKind: item.originAgentName ? "agent" : "system",
    kind: item.sourceKind,
    where: "in Work",
    text: item.subject.title ?? item.whyNow,
    at: item.activityAt,
    unread: false,
    source: { type: "attention", item },
  };
}

/** The Activity item id the same approval or question carries there, or null. */
function activityTwinId(item: AttentionItem): string | null {
  if (item.sourceKind === "approval") return approvalActivityId(item.approval?.id ?? item.subject.id);
  if (item.sourceKind === "issue_thread_interaction") {
    const issueId = item.interaction?.issueId ?? item.relatedIssue?.id;
    const interactionId = item.interaction?.id ?? item.subject.id;
    return issueId ? interactionActivityId(issueId, interactionId) : null;
  }
  return null;
}

function failedRunEntry(group: FailedRunGroup, agentName: (id: string | null | undefined) => string): ForYouEntry {
  const run = group.representative;
  return {
    id: `failed_run:${group.key}`,
    verb: "do",
    sourceId: `agent:${run.agentId ?? ""}`,
    sourceName: agentName(run.agentId),
    sourceKind: "agent",
    kind: "failed_run",
    where: "in Work",
    text: `${run.failureReason || "Heartbeat run failed"}${group.count > 1 ? ` ×${group.count}` : ""}`,
    at: run.startedAt ?? "",
    unread: false,
    source: { type: "failed_runs", group },
  };
}

const when = (iso: string) => Date.parse(iso) || 0;

/** Everything For you lists, one row per item, newest first (as Slack's Activity). */
export function forYouEntries(
  activity: readonly ActivityItem[],
  attention: readonly AttentionItem[],
  failedRuns: readonly FailedRunGroup[] = [],
  agentName: (id: string | null | undefined) => string = () => "An agent",
): ForYouEntry[] {
  const entries: ForYouEntry[] = [];
  for (const item of activity) {
    const verb = activityVerb(item);
    if (verb) entries.push(activityEntry(item, verb));
  }
  // An approval or question already listed by Activity is shown once; when Activity's
  // Paperclip half is unavailable the attention feed's copy is the only one, so it stays.
  const listed = new Set(activity.map((item) => item.id));
  for (const item of attention) {
    const verb = ATTENTION_VERBS[item.sourceKind];
    const twin = activityTwinId(item);
    if (verb && !(twin && listed.has(twin))) entries.push(attentionEntry(item, verb));
  }
  for (const group of failedRuns) entries.push(failedRunEntry(group, agentName));
  return entries.sort((a, b) => when(b.at) - when(a.at));
}

/**
 * What For you shows: everything, or one slice. `needs-you` is the actionable part; the
 * others are one kind of message. The same values back the filter chips and the
 * `?panel=activity&filter=` link.
 *
 * Under `needs-you` the attention desk (stock Paperclip's, with its shelves, snooze and
 * dismiss) lists every Paperclip decision, approval and question, so the rows here keep
 * what the desk does not carry: the Inbox's failed-run groups (with Retry; the desk
 * leaves its own failed runs out), assigned tasks, and session, login and extension asks.
 */
export type ForYouFilter = "all" | "needs-you" | "mentions" | "threads" | "dms";
export const FOR_YOU_FILTERS: readonly { id: ForYouFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "needs-you", label: "Needs you" },
  { id: "mentions", label: "Mentions" },
  { id: "threads", label: "Threads" },
  { id: "dms", label: "DMs" },
];

export function isForYouFilter(value: unknown): value is ForYouFilter {
  return FOR_YOU_FILTERS.some((filter) => filter.id === value);
}

/**
 * One chip's slice. `unreadOnly` (the Unread toggle) keeps unread messages and every
 * needs-you item: an open ask is still waiting on you after you have seen it.
 */
/** Shown by the attention desk under Needs you, so not repeated as a row there. */
function onAttentionDesk(entry: ForYouEntry): boolean {
  if (entry.source.type === "attention") return true;
  if (entry.source.type === "failed_runs") return false;
  const item = entry.source.item;
  return item.kind === "paperclip" && (item.subtype === "approval" || item.subtype === "interaction");
}

export function filterForYou(entries: readonly ForYouEntry[], filter: ForYouFilter, unreadOnly = false): ForYouEntry[] {
  const shown = unreadOnly ? entries.filter((entry) => entry.unread || entry.verb !== "reply") : entries;
  switch (filter) {
    case "needs-you": return shown.filter((entry) => entry.verb !== "reply" && !onAttentionDesk(entry));
    case "mentions": return shown.filter((entry) => entry.kind === "mention");
    case "threads": return shown.filter((entry) => entry.kind === "thread_reply");
    case "dms": return shown.filter((entry) => entry.kind === "dm");
    default: return shown.slice();
  }
}

// --- The team feed ---------------------------------------------------------

export type FeedTimelineEntry =
  | { type: "divider"; key: string; label: string }
  | { type: "post"; post: CanonicalRoomMessage };

function dayStart(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

export function dayLabel(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  const days = Math.round((dayStart(now) - dayStart(date)) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

/**
 * Top-level posts newest first. The ones after the reader's last look sit under
 * "New since you looked"; the rest under their day. A reader who never opened the
 * feed (`readSeq` 0) has nothing to compare against, so every post sits under its day.
 */
export function feedTimeline(messages: readonly CanonicalRoomMessage[], readSeq: number, now: Date = new Date()): FeedTimelineEntry[] {
  const posts = messages
    .filter((message) => message.threadRootMessageId === null && !message.deletedAt)
    .sort((a, b) => b.messageSeq - a.messageSeq);
  const out: FeedTimelineEntry[] = [];
  let previous: string | null = null;
  for (const post of posts) {
    const isNew = readSeq > 0 && post.messageSeq > readSeq;
    const label = isNew ? "New since you looked" : dayLabel(post.createdAt, now);
    const key = isNew ? "new" : `day:${label}`;
    if (key !== previous) out.push({ type: "divider", key, label });
    previous = key;
    out.push({ type: "post", post });
  }
  return out;
}

/** A post's first line as its title (markdown emphasis and heading marks dropped), and the rest. */
export function splitPost(text: string): { title: string; body: string } {
  const [first, ...rest] = text.trim().split("\n");
  const title = first.replace(/^#+\s*/, "").replace(/^(\*\*|__)(.*)\1$/, "$2").trim();
  return { title, body: rest.join("\n").trim() };
}
