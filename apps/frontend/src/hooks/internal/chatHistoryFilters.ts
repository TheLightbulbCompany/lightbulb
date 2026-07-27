// apps/frontend/src/hooks/internal/chatHistoryFilters.ts
//
// The visibility filter itself now lives in @isol8/shared (packages/shared/
// src/chat/history.ts) because mobile had a hand-ported copy of it that drifted
// — see that module's header. This file keeps the WEB-ONLY projection on top
// of it and re-exports the predicates so existing imports and tests are
// unaffected.

import type { HistoryMessage } from "@isol8/shared";
import { shouldHideHistoryMessage } from "@isol8/shared";
import { mergeStandaloneToolResults } from "./toolResultMerge";

export {
  isAssistantHeartbeatAck,
  isAssistantSilentReply,
  isEmptyUserTextOnlyMessage,
  isSyntheticExecWrapper,
  isSyntheticTranscriptRepairToolResult,
  shouldHideHistoryMessage,
} from "@isol8/shared";

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
