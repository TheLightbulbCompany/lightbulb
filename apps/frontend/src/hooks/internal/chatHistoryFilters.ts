// apps/frontend/src/hooks/internal/chatHistoryFilters.ts
//
// Mirrors upstream OpenClaw's `shouldHideHistoryMessage`, which lives at
// `ui/src/pages/chat/chat-history.ts` in the fork checkout (~/Desktop/agent).
// The old header pointed at `ui/src/ui/controllers/chat.ts`, a path that no
// longer exists — so nobody re-syncing ever diffed against the real thing,
// and this file's `isEmptyUserTextOnlyMessage` silently drifted into hiding
// every plain-text user message. Keep this pointer accurate.
//
// The load-bearing difference to preserve: upstream decides visibility on a
// message's EXTRACTED TEXT, never on whether `content` happens to be an array
// (`isTextOnlyContent` returns true for a bare string, then the verdict comes
// from `extractText`). See HistoryMessage.content in ./types for the three
// wire shapes that forces.
//
// Kept as an extractable helper so it's unit-testable and easy to
// re-sync if upstream's filter logic evolves.

import type { HistoryMessage } from "./types";
import { mergeStandaloneToolResults } from "./toolResultMerge";

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
  // A bare-string `content` IS the message body (see HistoryMessage.content).
  // Checked first, and load-bearing for isSyntheticExecWrapper below: OpenClaw's
  // post-exec wrapper messages arrive in exactly this shape, and until the
  // string case was handled they were only ever hidden as collateral damage of
  // isEmptyUserTextOnlyMessage's array check. With that check fixed, this is
  // the sole thing still keeping them out of the transcript.
  if (typeof m.content === "string") return m.content;
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
  // Decide on the message's TEXT, never on the shape of its container.
  //
  // This predicate used to open with `if (!Array.isArray(m.content)) return true`,
  // which reads OpenClaw's bare-string `content` — the shape it persists for
  // every plain-text turn — as "empty" and hid it. That silently deleted the
  // user's own words from every history refetch, and made the server-dispatched
  // onboarding kickoff invisible from the very first paint (it has no optimistic
  // local bubble to mask the loss, so Friday's transcript opened on her first
  // tool call with nothing to explain what she was answering).
  //
  // Upstream decides the same question on extracted text, not container shape
  // (`isEmptyUserTextOnlyMessage`, ui/src/pages/chat/chat-history.ts) — mirror it.
  if (typeof m.content === "string") return m.content.trim().length === 0;
  // No block array: the body may still be on the sibling `text` field.
  if (!Array.isArray(m.content) || m.content.length === 0) {
    return (m.text ?? "").trim().length === 0;
  }
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
  // Filter first (synthetic repair tool-results are dropped outright), then
  // fold the surviving standalone toolResult-role rows into tool_result
  // blocks on their assistant messages so they render as tool pills, never
  // as raw prose — see toolResultMerge.ts.
  return mergeStandaloneToolResults(
    messages.filter((m) => !shouldHideHistoryMessage(m)),
  );
}
