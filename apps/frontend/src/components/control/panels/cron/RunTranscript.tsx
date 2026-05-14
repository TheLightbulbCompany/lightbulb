"use client";

import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { MessageList } from "@/components/chat/MessageList";
import { buildChatItems } from "@/hooks/internal/buildChatItems";
import { groupBySide } from "@/hooks/internal/groupBySide";
import type { HistoryMessage } from "@/hooks/internal/types";
import { projectSessionMessages } from "./sessionMessageAdapter";

interface ChatHistoryResp {
  messages?: unknown[];
}

export function RunTranscript({
  sessionKey,
  afterTs,
  beforeTs,
}: {
  sessionKey: string | undefined;
  /**
   * When provided, filters transcript messages to those whose `timestamp`
   * lies in `[afterTs, beforeTs]`. Messages with no `timestamp` are
   * conservatively kept so older OpenClaw history formats (pre-timestamp)
   * still render. Used by RunDetailPanel to scope the transcript to a
   * specific run in multi-run sessions (non-isolated cron jobs share a
   * sessionKey across runs).
   */
  afterTs?: number;
  beforeTs?: number;
}) {
  // Always call the hook (hooks rules). Pass `null` method to skip the fetch
  // when we have no sessionKey — useGatewayRpc short-circuits SWR on null.
  const { data, error, isLoading, mutate } = useGatewayRpc<ChatHistoryResp>(
    sessionKey ? "chat.history" : null,
    sessionKey ? { sessionKey, limit: 200 } : undefined,
  );

  if (!sessionKey) {
    return (
      <div className="p-6 text-sm text-[#8a8578]">
        No transcript available for this run.
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="p-6 text-sm text-[#8a8578]">Loading transcript…</div>
    );
  }

  if (error) {
    return (
      <div className="p-6 text-sm text-destructive">
        Transcript unavailable: {String(error?.message ?? error)}.
        <button
          onClick={() => mutate()}
          className="ml-2 underline"
        >
          Retry
        </button>
      </div>
    );
  }

  // Project + scope the raw history payload BEFORE handing it to the
  // bubble pipeline so MessageList receives only the messages belonging
  // to this run. Order matters: hide-filter first (matches live chat),
  // then run-window scope, then buildChatItems + groupBySide.
  const projected = projectSessionMessages(data?.messages);
  const scoped = scopeMessagesToRun(projected, afterTs, beforeTs);
  if (scoped.length === 0) {
    return (
      <div className="p-6 text-sm text-[#8a8578]">
        No transcript available for this run.
      </div>
    );
  }
  const items = buildChatItems({
    chatMessages: scoped,
    chatStream: null,
    chatStreamRunId: null,
    chatStreamThinking: null,
    chatStreamThinkingRunId: null,
    chatStreamSegments: [],
    chatToolMessages: [],
    sessionKey,
  });
  const renderGroups = groupBySide(items);

  return (
    <MessageList renderGroups={renderGroups} isStreaming={false} isTyping={false} />
  );
}

/**
 * Filters `messages` to those whose `timestamp` is within
 * `[afterTs, beforeTs]`.
 *
 * - Messages with `timestamp === undefined` are kept conservatively
 *   (older history formats that predate the timestamp field).
 * - When `afterTs` is undefined, no lower bound is applied.
 * - When `beforeTs` is undefined, no upper bound is applied.
 */
export function scopeMessagesToRun(
  messages: HistoryMessage[],
  afterTs?: number,
  beforeTs?: number,
): HistoryMessage[] {
  if (afterTs === undefined && beforeTs === undefined) return messages;
  return messages.filter((m) => {
    if (m.timestamp === undefined) return true;
    if (afterTs !== undefined && m.timestamp < afterTs) return false;
    if (beforeTs !== undefined && m.timestamp > beforeTs) return false;
    return true;
  });
}

/**
 * Extracts the first text-only content from a HistoryMessage. Mirrors
 * what the old AdaptedMessage.content used to surface — concatenates
 * any "text" content blocks, falling back to a top-level `text` field.
 */
function extractUserText(m: HistoryMessage): string {
  if (Array.isArray(m.content)) {
    const parts: string[] = [];
    for (const b of m.content) {
      if (b && typeof b === "object" && b.type === "text" && typeof b.text === "string") {
        parts.push(b.text);
      }
    }
    if (parts.length > 0) return parts.join("");
  }
  return typeof m.text === "string" ? m.text : "";
}

/**
 * Returns the first user message text in `messages`.
 *
 * - When `afterTs` is provided, returns the first user message whose
 *   `timestamp` is `>= afterTs` (and `<= beforeTs` if provided). No
 *   tolerance window: in shared/non-isolated sessions with back-to-back
 *   manual reruns, a tolerance can let the previous run's prompt slip
 *   in. If no message has a `timestamp` in range, returns `undefined`
 *   (we deliberately do NOT fall back to the earliest-overall message
 *   — that was the old too-permissive behavior).
 * - When `afterTs` is undefined, returns the first user message overall
 *   without any `timestamp` check.
 */
export function firstUserMessage(
  messages: HistoryMessage[],
  afterTs?: number,
  beforeTs?: number,
): string | undefined {
  if (afterTs !== undefined) {
    const scoped = messages.find(
      (m) =>
        m.role === "user" &&
        m.timestamp !== undefined &&
        m.timestamp >= afterTs &&
        (beforeTs === undefined || m.timestamp <= beforeTs),
    );
    return scoped ? extractUserText(scoped) || undefined : undefined;
  }
  const first = messages.find((m) => m.role === "user");
  return first ? extractUserText(first) || undefined : undefined;
}
