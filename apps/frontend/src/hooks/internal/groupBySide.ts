// apps/frontend/src/hooks/internal/groupBySide.ts
//
// Post-process buildChatItems' output: wrap consecutive items belonging
// to the same visual side (user vs assistant) into one RenderGroup.
// Mirrors upstream's `groupMessages` at
// `~/Desktop/openclaw/ui/src/ui/chat/build-chat-items.ts:151-200`.

import type { ChatRenderItem, RenderGroup, RenderSide, HistoryMessage } from "./types";

function lowerRole(m: HistoryMessage): string {
  return typeof m.role === "string" ? m.role.toLowerCase() : "";
}

function sideOf(item: ChatRenderItem): RenderSide {
  if (item.kind === "message") {
    return lowerRole(item.message) === "user" ? "user" : "assistant";
  }
  // stream-segment, stream, live-thinking, divider — all assistant-side.
  return "assistant";
}

export function groupBySide(items: ChatRenderItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  for (const item of items) {
    const side = sideOf(item);
    const last = groups[groups.length - 1];
    if (last && last.side === side) {
      last.items.push(item);
    } else {
      groups.push({
        side,
        key: `group:${groups.length}:${item.key}`,
        items: [item],
      });
    }
  }
  return groups;
}
