// apps/frontend/src/hooks/internal/chatHistoryFilters.ts
//
// Mirrors upstream OpenClaw's `shouldHideHistoryMessage` at
// `~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:225-232`.
// Kept as an extractable helper so it's unit-testable and easy to
// re-sync if upstream's filter logic evolves.

import type { HistoryMessage } from "./types";

const SILENT_REPLY_TOKEN = "NO_REPLY";
const HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK";
const SYNTHETIC_REPAIR_TOKEN = "SYNTHETIC_TRANSCRIPT_REPAIR_RESULT";

// OpenClaw injects a post-exec "wrapper" message with role:"user" after a
// tool/exec finishes — an LLM-facing blob (exec footer, raw stdout, and a
// "continue the task" instruction) that has no business rendering as a user
// bubble. These three substrings are distinctive enough to detect the
// template without matching genuine user prose. Extend the list if a new
// upstream wrapper variant surfaces.
const SYNTHETIC_EXEC_WRAPPER_MARKERS = [
  "Exec finished (gateway id=",
  "Exact completion details:",
  "Continue the task if needed, then reply to the user",
] as const;

function extractFirstText(m: HistoryMessage): string {
  if (typeof m.text === "string") return m.text;
  if (!Array.isArray(m.content)) return "";
  for (const b of m.content) {
    if (b && typeof b === "object" && typeof b.text === "string") {
      return b.text;
    }
  }
  return "";
}

function lowerRole(m: HistoryMessage): string {
  return typeof m.role === "string" ? m.role.toLowerCase() : "";
}

export function isAssistantSilentReply(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "assistant") return false;
  return extractFirstText(m).trim() === SILENT_REPLY_TOKEN;
}

export function isAssistantHeartbeatAck(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "assistant") return false;
  return extractFirstText(m).trim() === HEARTBEAT_OK_TOKEN;
}

export function isSyntheticTranscriptRepairToolResult(m: HistoryMessage): boolean {
  const role = lowerRole(m);
  if (role !== "toolresult" && role !== "tool_result" && role !== "tool") return false;
  if (!Array.isArray(m.content)) return false;
  for (const b of m.content) {
    if (!b || typeof b !== "object") continue;
    const c = b.content ?? b.result;
    if (typeof c === "string" && c.includes(SYNTHETIC_REPAIR_TOKEN)) return true;
  }
  return false;
}

export function isEmptyUserTextOnlyMessage(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "user") return false;
  if (!Array.isArray(m.content) || m.content.length === 0) return true;
  // Reject if any non-text block exists (attachments, images, etc.)
  const allTextBlocks = m.content.every(
    (b) => b && typeof b === "object" && typeof b.type === "string" &&
      (b.type === "text" || b.type === "input_text" || b.type === "output_text"),
  );
  if (!allTextBlocks) return false;
  // All text blocks must be empty after trim.
  return m.content.every(
    (b) => typeof b.text !== "string" || b.text.trim().length === 0,
  );
}

export function isSyntheticExecWrapper(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "user") return false;
  const text = extractFirstText(m);
  if (!text) return false;
  return SYNTHETIC_EXEC_WRAPPER_MARKERS.some((marker) => text.includes(marker));
}

export function shouldHideHistoryMessage(m: HistoryMessage): boolean {
  return (
    isAssistantSilentReply(m) ||
    isAssistantHeartbeatAck(m) ||
    isSyntheticTranscriptRepairToolResult(m) ||
    isEmptyUserTextOnlyMessage(m) ||
    isSyntheticExecWrapper(m)
  );
}

export function projectChatHistoryMessages(
  messages: HistoryMessage[],
): HistoryMessage[] {
  return messages.filter((m) => !shouldHideHistoryMessage(m));
}
