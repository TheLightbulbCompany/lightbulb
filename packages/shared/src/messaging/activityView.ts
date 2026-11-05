/**
 * How an Activity list is shown, shared by web and mobile so both read the
 * same: the filter chips, the "Needs you" pin, the Today / Earlier groups and
 * the Slack-style row line (bold actor, plain verb phrase, one-line snippet,
 * where it happened). Pure: no React, no DOM, no react-native.
 */
import { notificationLine, notificationTitle, sentenceCase } from "./notificationCopy";
import type { ActivityItem, ActivityLinkKind } from "./types";

export type ActivityFilter = "all" | "needs-you" | "mentions" | "threads" | "agents";

export const ACTIVITY_FILTERS: readonly { id: ActivityFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "needs-you", label: "Needs you" },
  { id: "mentions", label: "Mentions" },
  { id: "threads", label: "Threads" },
  { id: "agents", label: "Agents’ work" },
];

/** Pinned and always on the badge (the server's `needs_you`): an open approval, an agent's pending question,
 * a failed run, one of your shared sessions still waiting for you, or a teammate asking to join one. */
export function isNeedsYou(item: ActivityItem): boolean {
  // A super-agent question reaches only its answerers (activity.py `needs_you`): open = unanswered.
  if (item.kind === "task_question") return item.state === "open";
  if (item.kind === "session") return item.state === "open" && (item.subtype === "waiting" || item.subtype === "join_request");
  if (item.kind === "login" || item.kind === "extension" || item.kind === "reconnect") return item.state === "open";
  return item.kind === "paperclip" && item.state === "open"
    && (item.subtype === "approval" || item.subtype === "interaction" || item.subtype === "failed_run");
}

export function filterActivity(items: readonly ActivityItem[], filter: ActivityFilter): ActivityItem[] {
  switch (filter) {
    case "needs-you": return items.filter(isNeedsYou);
    case "mentions": return items.filter((item) => item.kind === "mention");
    case "threads": return items.filter((item) => item.kind === "thread_reply");
    // Work the agents did or handed you: tasks, runs, approvals already decided.
    case "agents": return items.filter((item) => item.kind === "paperclip" && !isNeedsYou(item));
    default: return items.slice();
  }
}

export interface ActivitySection {
  key: "needs-you" | "today" | "earlier";
  title: string;
  items: ActivityItem[];
}

function sameLocalDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** "Needs you" pinned first (server order), then the rest split by local day. Empty sections are dropped. */
export function groupActivity(items: readonly ActivityItem[], now: Date = new Date()): ActivitySection[] {
  const pinned: ActivityItem[] = [];
  const today: ActivityItem[] = [];
  const earlier: ActivityItem[] = [];
  for (const item of items) {
    if (isNeedsYou(item)) pinned.push(item);
    else if (sameLocalDay(new Date(item.createdAt), now)) today.push(item);
    else earlier.push(item);
  }
  const sections: ActivitySection[] = [
    { key: "needs-you", title: "Needs you", items: pinned },
    { key: "today", title: "Today", items: today },
    { key: "earlier", title: "Earlier", items: earlier },
  ];
  return sections.filter((section) => section.items.length > 0);
}

export const ACTIVITY_EMPTY_COPY: Readonly<Record<ActivityFilter, string>> = {
  all: "You’re all caught up.",
  "needs-you": "Nothing is waiting on you. Approvals, agents’ questions and failed runs land here.",
  mentions: "No mentions yet. When someone @mentions you, it shows up here.",
  threads: "No thread replies. Replies in threads you’re part of show up here.",
  "agents": "No agent work to show. Tasks assigned to you and finished decisions land here.",
};

export interface ActivityRowParts {
  /** Bold, when the sentence starts with the actor ("Ann"); null when it does not. */
  lead: string | null;
  /** The rest of the sentence, verbatim ("mentioned you in #general", "'s last run failed"). */
  rest: string;
  /** One line: the message, the ask's summary, or its first detail. */
  snippet: string | null;
  /** Where it happened (#general, LB-12), unless the sentence already says so. */
  location: string | null;
  /** The link `location` names, so a client can open it. */
  locationLink: { kind: ActivityLinkKind; id: string } | null;
}

const LOCATION_KINDS: readonly ActivityLinkKind[] = ["room", "issue"];

/**
 * Splits the server's sentence into Slack's row shape without composing new
 * text: "Growth: Pilot X auto-replies" reads "**Growth** needs your approval ·
 * Pilot X auto-replies", "Ann mentioned you in #general" keeps its own verb.
 */
export function activityRowParts(item: ActivityItem): ActivityRowParts {
  const { summary } = item;
  const name = item.actor.name;
  let lead: string | null = null;
  let rest = summary.title;
  let subject: string | null = null;
  if (name && summary.title.startsWith(name)) {
    lead = name;
    rest = summary.title.slice(name.length);
    if (rest.startsWith(": ")) {
      subject = rest.slice(2);
      rest = item.subtype === "approval" ? " needs your approval" : ":";
    }
    rest = rest.startsWith("'") || rest.startsWith("’") ? rest : rest.trimStart();
  }
  const field = summary.preview?.fields[0];
  // The why before the payload: an approval's payload shows in full below its row.
  const detail = summary.reason ?? summary.preview?.text ?? (field ? `${field.label}: ${field.value}` : null);
  const snippet = subject ? [subject, summary.reason].filter(Boolean).join(" — ") : detail;
  const link = summary.links.find((entry) => LOCATION_KINDS.includes(entry.kind));
  // A direct message has no "where": its title says who, and the place is the
  // two of you ("sent you a direct message · your conversation").
  const shown = link && item.kind !== "dm" && !summary.title.includes(link.label) ? link : null;
  return {
    lead,
    rest,
    snippet: snippet || null,
    location: shown?.label ?? null,
    locationLink: shown ? { kind: shown.kind, id: shown.id } : null,
  };
}

/** The Activity item id of a Paperclip approval: what a notification focuses. */
export function approvalActivityId(approvalId: string): string {
  return `paperclip:approval:${approvalId}`;
}

/** The Activity item id of an agent's pending question (a Paperclip issue-thread interaction). */
export function interactionActivityId(issueId: string, interactionId: string): string {
  return `paperclip:interaction:${issueId}:${interactionId}`;
}

/**
 * "Growth · LB-12" / "Needs your confirmation: Launch email" from the rendered
 * item -- the same rule as the backend push (`approval_push_copy`), so the phone
 * and the desktop agree. Line 1 names the task when there is one.
 */
export function approvalNotificationCopy(item: ActivityItem): { title: string; body: string } {
  const name = item.actor.name;
  const title = item.summary.title;
  let what: string;
  if (title.startsWith(`${name}: `)) what = `Needs your approval: ${title.slice(name.length + 2)}`;
  else if (title.startsWith(`${name} `)) what = sentenceCase(title.slice(name.length + 1));
  else what = `Needs your approval: ${title}`;
  const where = item.summary.links.find((link) => link.kind === "issue")?.label
    ?? (item.subtype === "approval" ? "Approval" : null);
  return { title: notificationTitle(name, where), body: notificationLine(what) };
}
