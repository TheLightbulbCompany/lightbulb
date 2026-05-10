import * as React from "react";
import { AgentGlyph, type ToolUse } from "./MessageList";

interface StreamingStatusBarProps {
  isStreaming: boolean;
  /** toolUses from the last assistant message; used to surface the in-flight tool. */
  toolUses?: ToolUse[];
}

function deriveLabel(toolUses: ToolUse[] | undefined): string {
  if (!toolUses || toolUses.length === 0) return "Thinking";
  for (let i = toolUses.length - 1; i >= 0; i--) {
    const t = toolUses[i];
    if (t.status === "pending-approval") return `Awaiting approval: ${t.tool}`;
    if (t.status === "running") return `Running ${t.tool}`;
  }
  return "Thinking";
}

export function StreamingStatusBar({ isStreaming, toolUses }: StreamingStatusBarProps) {
  if (!isStreaming) return null;
  const label = deriveLabel(toolUses);
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
