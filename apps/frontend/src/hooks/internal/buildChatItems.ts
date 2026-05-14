// apps/frontend/src/hooks/internal/buildChatItems.ts
//
// Build a flat render-item list from useAgentChat's state. Mirrors
// upstream's `~/Desktop/openclaw/ui/src/ui/chat/build-chat-items.ts:249-364`
// with one addition: a `live-thinking` item kind (Isol8 keeps live
// thinking, upstream drops it).

import type { ChatRenderItem, HistoryMessage } from "./types";

export interface ChatItemsState {
  chatMessages: HistoryMessage[];
  chatStream: string | null;
  chatStreamRunId: string | null;
  chatStreamThinking: string | null;
  chatStreamThinkingRunId: string | null;
  chatStreamSegments: Array<{ text: string; ts: number }>;
  chatToolMessages: HistoryMessage[];
  sessionKey: string;
}

const COMPACTION_LABEL = "Context compacted";

function isCompactionMessage(m: HistoryMessage): boolean {
  return m.__openclaw?.kind === "compaction";
}

function messageKey(m: HistoryMessage, idx: number, prefix: string): string {
  const ts = m.timestamp ?? idx;
  return `${prefix}:${idx}:${ts}`;
}

export function buildChatItems(state: ChatItemsState): ChatRenderItem[] {
  const items: ChatRenderItem[] = [];

  // 1. History messages — each becomes one ChatRenderItem.
  state.chatMessages.forEach((m, idx) => {
    if (isCompactionMessage(m)) {
      items.push({
        kind: "divider",
        key: `divider:${idx}:${m.timestamp ?? idx}`,
        label: COMPACTION_LABEL,
      });
    } else {
      items.push({
        kind: "message",
        key: messageKey(m, idx, `msg:${state.sessionKey}`),
        message: m,
      });
    }
  });

  // 2. Interleave chatStreamSegments and chatToolMessages by index.
  //    Upstream pattern: when a tool interrupts streaming, chatStream is
  //    flushed into segments and a new tool message begins. So segments[i]
  //    is the text-accumulated-before-tool[i].
  const maxLen = Math.max(
    state.chatStreamSegments.length,
    state.chatToolMessages.length,
  );
  for (let i = 0; i < maxLen; i++) {
    const seg = state.chatStreamSegments[i];
    if (seg && seg.text.length > 0) {
      items.push({
        kind: "stream-segment",
        key: `seg:${state.sessionKey}:${i}:${seg.ts}`,
        text: seg.text,
        ts: seg.ts,
      });
    }
    const tool = state.chatToolMessages[i];
    if (tool) {
      items.push({
        kind: "message",
        key: messageKey(tool, i, `tool:${state.sessionKey}`),
        message: tool,
      });
    }
  }

  // 3. Live thinking (our addition) at the tail.
  if (state.chatStreamThinking && state.chatStreamThinking.length > 0) {
    items.push({
      kind: "live-thinking",
      key: `live-think:${state.chatStreamThinkingRunId ?? "live"}`,
      text: state.chatStreamThinking,
      runId: state.chatStreamThinkingRunId ?? "",
    });
  }

  // 4. Live chatStream at the tail.
  if (state.chatStream && state.chatStream.length > 0) {
    items.push({
      kind: "stream",
      key: `stream:${state.chatStreamRunId ?? "live"}`,
      text: state.chatStream,
      runId: state.chatStreamRunId ?? "",
    });
  }

  return items;
}
