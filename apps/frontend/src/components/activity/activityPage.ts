/**
 * The Activity page's arrangement, pure so it is tested on its own: "For you"
 * (what needs this person, by verb, one row per source) and the team feed's
 * dividers (new since you looked, then days).
 */
import { activityRowParts, isNeedsYou, type ActivityItem, type CanonicalRoomMessage } from "@lightbulb/shared";
import type { FailedRunGroup } from "@/components/teams/inbox/lib/groupFailedRuns";
import type { AttentionItem } from "@/components/teams/shared/types";

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
  /** What the item is (`approval`, `mention`, `failed_run`…): names a collapsed row's items. */
  kind: string;
  text: string;
  source: ForYouSource;
}

/** One line in a group: a source's items, collapsed ("Anna · 3 messages") when there are several. */
export interface ForYouRow {
  key: string;
  sourceName: string;
  sourceKind: ForYouEntry["sourceKind"];
  sourceId: string;
  entries: ForYouEntry[];
}

export interface ForYouGroup {
  verb: ForYouVerb;
  label: string;
  count: number;
  rows: ForYouRow[];
}

const LABELS: Record<ForYouVerb, string> = { decide: "Decide", reply: "Reply", do: "Do" };
/** What a collapsed row counts when its items are mixed: "3 decisions", "3 to do". */
const VERB_NOUNS: Record<ForYouVerb, [one: string, many: string]> = {
  decide: ["decision", "decisions"],
  reply: ["message", "messages"],
  do: ["thing to do", "things to do"],
};

/** …and when they are all one kind: "3 approvals", "3 messages", "3 failed runs". */
const KIND_NOUNS: Record<string, [one: string, many: string]> = {
  approval: ["approval", "approvals"],
  interaction: ["question", "questions"],
  agent_ask: ["question", "questions"],
  decision: ["question", "questions"],
  mention: ["message", "messages"],
  dm: ["message", "messages"],
  thread_reply: ["message", "messages"],
  assigned_issue: ["task", "tasks"],
  failed_run: ["failed run", "failed runs"],
  join_request: ["request", "requests"],
  login: ["login request", "login requests"],
  extension: ["extension request", "extension requests"],
};

/** "3 approvals" when every item names the same thing, else the verb's word ("3 things to do"). */
export function rowSummary(row: ForYouRow): string {
  const nouns = row.entries.map((entry) => KIND_NOUNS[entry.kind]);
  const same = nouns.every((noun) => noun && noun[1] === nouns[0]?.[1]);
  const [one, many] = same ? nouns[0]! : VERB_NOUNS[row.entries[0].verb];
  return `${row.entries.length} ${row.entries.length === 1 ? one : many}`;
}

/**
 * DECIDE: approvals and agents' questions. REPLY: mentions, DMs and thread replies not yet
 * read. DO: assigned tasks and the rest of "needs you" (session, browser and
 * extension asks). Anything else (a reaction, a finished run, a decided approval) is not
 * for you to act on: null.
 */
export function activityVerb(item: ActivityItem): ForYouVerb | null {
  if (item.state !== "open") return null;
  if (item.kind === "mention" || item.kind === "dm" || item.kind === "thread_reply") return item.read ? null : "reply";
  if (item.kind === "paperclip" && item.subtype === "assigned_issue") return "do";
  // Failed runs come from the Inbox's own list (every failure, with Retry and Dismiss), not this one.
  if (item.kind === "paperclip" && item.subtype === "failed_run") return null;
  if (!isNeedsYou(item)) return null;
  if (item.subtype === "approval" || item.subtype === "interaction" || item.subtype === "agent_ask") return "decide";
  return "do";
}

/**
 * The Teams attention kinds the Activity feed does not carry itself. Approvals and agents'
 * questions (`issue_thread_interaction`) are already Activity items, so they are left out
 * here rather than shown twice.
 */
const ATTENTION_VERBS: Partial<Record<AttentionItem["sourceKind"], ForYouVerb>> = {
  decision: "decide",
  join_request: "do",
  review: "do",
  blocker_attention: "do",
  recovery_action: "do",
};

function activityEntry(item: ActivityItem, verb: ForYouVerb): ForYouEntry {
  const parts = activityRowParts(item);
  return {
    id: item.id,
    verb,
    sourceId: `${item.actor.kind}:${item.actor.id}`,
    sourceName: item.actor.name,
    sourceKind: item.actor.kind,
    kind: item.subtype ?? item.kind,
    // "Ann · <her message>", "Finance · Pilot X auto-replies"; a sentence that does not
    // start with the actor ("LB-12 is assigned to you") is shown whole.
    text: parts.lead ? parts.snippet ?? parts.rest : item.summary.title,
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
    text: item.subject.title ?? item.whyNow,
    source: { type: "attention", item },
  };
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
    text: `${run.failureReason || "Heartbeat run failed"}${group.count > 1 ? ` ×${group.count}` : ""}`,
    source: { type: "failed_runs", group },
  };
}

/** Everything For you lists, in the sources' own order (needs-you first, then newest). */
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
  for (const item of attention) {
    const verb = ATTENTION_VERBS[item.sourceKind];
    if (verb) entries.push(attentionEntry(item, verb));
  }
  for (const group of failedRuns) entries.push(failedRunEntry(group, agentName));
  return entries;
}

/** Groups by verb (Decide, Reply, Do; empty ones dropped), then one row per source within a verb. */
export function groupForYou(entries: readonly ForYouEntry[]): ForYouGroup[] {
  return (["decide", "reply", "do"] as const).flatMap((verb) => {
    const rows = new Map<string, ForYouRow>();
    for (const entry of entries) {
      if (entry.verb !== verb) continue;
      const row = rows.get(entry.sourceId);
      if (row) row.entries.push(entry);
      else rows.set(entry.sourceId, { key: `${verb}:${entry.sourceId}`, sourceName: entry.sourceName, sourceKind: entry.sourceKind, sourceId: entry.sourceId, entries: [entry] });
    }
    const list = [...rows.values()];
    const count = list.reduce((sum, row) => sum + row.entries.length, 0);
    return count ? [{ verb, label: LABELS[verb], count, rows: list }] : [];
  });
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
