import * as React from "react";
import { AgentGlyph, type Message } from "./MessageList";

interface StreamingStatusBarProps {
  isStreaming: boolean;
  messages: Message[];
}

export function deriveStreamingLabel(messages: Message[]): string {
  // Pending approvals are surfaced by ExecApprovalCards (rendered above
  // ChatInput, fed by useExecApprovals). They no longer flow through
  // messages.toolUses, so this label only reflects running tool execution
  // and the "thinking" idle state.
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "assistant") continue;
    const tools = m.toolUses ?? [];
    for (let j = tools.length - 1; j >= 0; j--) {
      if (tools[j].status === "running") return `Running ${tools[j].tool}`;
    }
    break;
  }
  return "Thinking";
}

export function StreamingStatusBar({ isStreaming, messages }: StreamingStatusBarProps) {
  if (!isStreaming) return null;
  const label = deriveStreamingLabel(messages);
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
