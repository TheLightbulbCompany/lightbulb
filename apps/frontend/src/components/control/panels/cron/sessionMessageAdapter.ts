// apps/frontend/src/components/control/panels/cron/sessionMessageAdapter.ts
//
// Cron-run transcripts reuse MessageList, which consumes `RenderGroup[]` —
// the same shape useAgentChat produces for the live chat.
//
// This adapter wires `chat.history` payloads (cron run sessions) through
// the same buildChatItems + groupBySide pipeline so the transcript view
// renders identically to the live chat. Single source of truth for chat
// rendering.

import { projectChatHistoryMessages } from "@/hooks/internal/chatHistoryFilters";
import { buildChatItems } from "@/hooks/internal/buildChatItems";
import { groupBySide } from "@/hooks/internal/groupBySide";
import type { RenderGroup, HistoryMessage, ContentBlock } from "@/hooks/internal/types";

/**
 * Coerces an unknown payload entry into a HistoryMessage. Returns
 * undefined for entries we definitely can't render (missing role,
 * non-objects). The underlying `chat.history` shape varies slightly
 * across OpenClaw versions, so this stays defensive.
 *
 * Note: we read `m.timestamp` first then fall back to `m.ts` — older
 * payloads used `ts`, newer ones use `timestamp` (matches HistoryMessage).
 */
function toHistoryMessage(item: unknown): HistoryMessage | undefined {
  if (!item || typeof item !== "object") return undefined;
  const m = item as Record<string, unknown>;
  if (typeof m.role !== "string") return undefined;
  const content = Array.isArray(m.content)
    ? (m.content as ContentBlock[])
    : undefined;
  const tsRaw =
    typeof m.timestamp === "number"
      ? m.timestamp
      : typeof m.ts === "number"
        ? m.ts
        : undefined;
  return {
    role: m.role,
    content,
    text: typeof m.text === "string" ? m.text : undefined,
    timestamp: typeof tsRaw === "number" && Number.isFinite(tsRaw) ? tsRaw : undefined,
    runId: typeof m.runId === "string" ? m.runId : undefined,
    toolCallId: typeof m.toolCallId === "string" ? m.toolCallId : undefined,
    tool_call_id:
      typeof m.tool_call_id === "string" ? m.tool_call_id : undefined,
    toolName: typeof m.toolName === "string" ? m.toolName : undefined,
    tool_name: typeof m.tool_name === "string" ? m.tool_name : undefined,
    errorCode: typeof m.errorCode === "string" ? m.errorCode : undefined,
    __openclaw:
      m.__openclaw && typeof m.__openclaw === "object"
        ? (m.__openclaw as { kind?: string })
        : undefined,
  };
}

/**
 * Coerces an unknown `chat.history` payload into a normalized
 * `HistoryMessage[]`, then applies the same hide-filter MessageList uses
 * in the live chat. Used by RunTranscript (for the bubble pipeline) and
 * RunDetailPanel (for "first user message" prompt extraction).
 */
export function projectSessionMessages(raw: unknown[] | undefined): HistoryMessage[] {
  if (!raw) return [];
  const messages: HistoryMessage[] = [];
  for (const item of raw) {
    const m = toHistoryMessage(item);
    if (m) messages.push(m);
  }
  return projectChatHistoryMessages(messages);
}

/**
 * Adapts a raw `chat.history` payload into the `RenderGroup[]` shape
 * MessageList consumes. Reuses the live-chat pipeline so transcript
 * rendering stays in lockstep with the chat view.
 *
 * `sessionKey` is forwarded to buildChatItems so item keys stay stable
 * across re-renders (same as useAgentChat).
 */
export function adaptSessionMessages(
  raw: unknown[] | undefined,
  sessionKey: string,
): RenderGroup[] {
  const projected = projectSessionMessages(raw);
  const items = buildChatItems({
    chatMessages: projected,
    chatStream: null,
    chatStreamRunId: null,
    chatStreamThinking: null,
    chatStreamThinkingRunId: null,
    chatStreamSegments: [],
    chatToolMessages: [],
    sessionKey,
  });
  return groupBySide(items);
}
