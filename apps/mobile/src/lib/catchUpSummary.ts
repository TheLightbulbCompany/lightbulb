import type { CanonicalRoomMessage } from "@lightbulb/shared";

export const RECENT_SUMMARY_LIMIT = 40;
export const UNREAD_SUMMARY_LIMIT = 80;

export interface CatchUpSummary {
  scope: string;
  messageCount: number;
  mainDiscussion: string[];
  decisions: string[];
  actionItems: string[];
  openQuestions: string[];
}

function clean(message: CanonicalRoomMessage): string | null {
  if (message.deletedAt || !message.text.trim()) return null;
  return `${message.author.displayName}: ${message.text.trim().replace(/\s+/g, " ")}`;
}

function unique(items: string[], limit: number): string[] {
  return [...new Set(items)].slice(0, limit);
}

/**
 * Builds an intentionally extractive summary from messages the room machine has
 * already authorized and loaded. Keeping this local means the action never
 * posts a message or creates a hidden agent turn. The section labels make the
 * distinction between stated decisions and still-open questions explicit.
 */
export function buildCatchUpSummary(
  messages: CanonicalRoomMessage[],
  readSeq: number | null,
): CatchUpSummary {
  const visible = messages
    .filter((message) => !message.deletedAt && message.text.trim())
    .sort((left, right) => left.messageSeq - right.messageSeq);

  const hasReliableCursor = readSeq !== null && Number.isSafeInteger(readSeq) && readSeq >= 0;
  const candidates = hasReliableCursor
    ? visible.filter((message) => message.messageSeq > readSeq).slice(-UNREAD_SUMMARY_LIMIT)
    : visible.slice(-RECENT_SUMMARY_LIMIT);
  const bounded = hasReliableCursor
    && visible.filter((message) => message.messageSeq > readSeq).length > candidates.length;

  const lines = candidates.map(clean).filter((line): line is string => line !== null);
  const decisions = candidates
    .filter((message) => /\b(decided|decision|agreed|approved|confirmed|settled on)\b/i.test(message.text))
    .map(clean)
    .filter((line): line is string => line !== null);
  const actionItems = candidates
    .filter((message) => (
      /\b(action item|todo|to-do|need to|needs to|will|should|please|follow up|follow-up)\b/i
        .test(message.text)
    ))
    .map(clean)
    .filter((line): line is string => line !== null);
  const openQuestions = candidates
    .filter((message) => message.text.includes("?") && !/\b(resolved|answered)\b/i.test(message.text))
    .map(clean)
    .filter((line): line is string => line !== null);

  const scope = hasReliableCursor
    ? bounded
      ? `Latest ${UNREAD_SUMMARY_LIMIT} loaded messages since you last viewed this chat`
      : "Messages since you last viewed this chat"
    : `Recent messages (up to ${RECENT_SUMMARY_LIMIT}; no reliable read position was available)`;

  return {
    scope,
    messageCount: candidates.length,
    mainDiscussion: unique(lines, 5),
    decisions: unique(decisions, 4),
    actionItems: unique(actionItems, 4),
    openQuestions: unique(openQuestions, 4),
  };
}
