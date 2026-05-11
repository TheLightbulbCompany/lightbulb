import * as React from "react";
import { AgentGlyph, type Message } from "./MessageList";

interface StreamingStatusBarProps {
  isStreaming: boolean;
  messages: Message[];
}

export function deriveStreamingLabel(messages: Message[]): string {
  // Approvals block the agent run — surface any pending approval anywhere
  // in the conversation, not just the latest bubble. An approval can land
  // on an earlier assistant bubble after a newer bubble has already been
  // created (see useAgentChat test "approval.requested event matches
  // toolCallId across any assistant bubble"), so scoping this to the last
  // bubble would hide what's actually blocking the user.
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "assistant" || !m.toolUses) continue;
    for (const t of m.toolUses) {
      if (t.status === "pending-approval") {
        return `Awaiting approval: ${t.tool}`;
      }
    }
  }
  // Running tools always sit on the most recent assistant bubble (a new
  // run can't start while an approval is pending).
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
