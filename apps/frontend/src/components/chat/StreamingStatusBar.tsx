import * as React from "react";
import { AgentGlyph } from "./MessageList";
import type { ChatRenderItem, ContentBlock } from "@/hooks/internal/types";

interface StreamingStatusBarProps {
  isStreaming: boolean;
  renderItems: ChatRenderItem[];
}

export function deriveStreamingLabel(items: ChatRenderItem[]): string {
  // Pending approvals are surfaced by ExecApprovalCards (rendered above
  // ChatInput, fed by useExecApprovals). They no longer flow through this
  // status bar, so this label only reflects running tool execution and the
  // "thinking" idle state.
  //
  // Walk the render items in reverse looking for the most recent
  // assistant-side message item and check its content blocks for a
  // tool_use without a matching tool_result (still running).
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.kind !== "message") continue;
    if (item.message.role !== "assistant") continue;
    const blocks: ContentBlock[] = Array.isArray(item.message.content)
      ? item.message.content
      : [];
    for (let j = blocks.length - 1; j >= 0; j--) {
      const b = blocks[j];
      if (b?.type !== "tool_use") continue;
      const hasResult = blocks.some(
        (r) => r?.type === "tool_result" && r.tool_use_id === b.id,
      );
      if (!hasResult) return `Running ${b.name ?? "tool"}`;
    }
    // First assistant message we saw had no running tool — stop searching.
    break;
  }
  return "Thinking";
}

export function StreamingStatusBar({ isStreaming, renderItems }: StreamingStatusBarProps) {
  if (!isStreaming) return null;
  const label = deriveStreamingLabel(renderItems);
  return (
    <div
      data-testid="streaming-status-bar"
      className="flex justify-center pointer-events-none px-4 pt-2 pb-1"
    >
      <div className="flex items-center gap-2 text-xs text-[#8a8578] bg-[#f3efe6]/80 backdrop-blur-sm rounded-full px-3 py-1.5 shadow-sm">
        <AgentGlyph state="thinking" size={16} />
        <span className="font-medium tracking-tight">{label}…</span>
      </div>
    </div>
  );
}
