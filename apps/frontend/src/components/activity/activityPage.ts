/**
 * The Activity page's arrangement, pure so it is tested on its own: "For you"
 * (one row per item, newest first, Slack's Activity tab) and the team feed's
 * dividers (new since you looked, then days).
 */
import { activityRowParts, isNeedsYou, type ActivityItem, type CanonicalRoomMessage } from "@lightbulb/shared";

/** `reply`: a message (mention, DM, thread reply). `decide` / `do`: needs you. */
export type ForYouVerb = "decide" | "reply" | "do";

export type ForYouSource = { type: "activity"; item: ActivityItem };

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

const when = (iso: string) => Date.parse(iso) || 0;

/** Everything For you lists, one row per item, newest first (as Slack's Activity). */
export function forYouEntries(activity: readonly ActivityItem[]): ForYouEntry[] {
  const entries: ForYouEntry[] = [];
  for (const item of activity) {
    const verb = activityVerb(item);
    if (verb) entries.push(activityEntry(item, verb));
  }
  return entries.sort((a, b) => when(b.at) - when(a.at));
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

// --- Home (super-agent) ------------------------------------------------------

/**
 * What For you carried that Home shows nowhere else: unread mentions and replies to you (one compact line), and open
 * asks that are not requests (a website login, a browser extension, a reconnect, a session waiting on you). Request
 * questions are Needs you rows; DMs show as unread in the sidebar; Paperclip items are retired here.
 */
export function homeExtras(items: readonly ActivityItem[]): { mentions: ActivityItem[]; asks: ActivityItem[] } {
  const mentions = items.filter((item) => item.state === "open" && !item.read && (item.kind === "mention" || item.kind === "thread_reply"));
  const asks = items.filter((item) => item.kind !== "task_question" && item.kind !== "paperclip"
    && (activityVerb(item) === "do" || activityVerb(item) === "decide"));
  return { mentions, asks };
}
