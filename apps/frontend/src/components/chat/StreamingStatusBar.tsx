import * as React from "react";
import { AgentGlyph } from "./MessageList";

interface StreamingStatusBarProps {
  isStreaming: boolean;
  /**
   * Server-truth "run is active" signal (see useAgentChat's `busy`), independent
   * of whether tokens are actively streaming in this tab. Keeps the bar lit
   * across silent windows of a live run — long tool execution, the beat before
   * the next tokens, the tool-approval pause — where isStreaming alone would
   * have gone dark.
   */
  active?: boolean;
}

/**
 * The "something is happening" bar, shown under the transcript.
 *
 * It used to derive its own label by walking the render items backwards for an
 * unfinished tool call and printing `Running ${b.name}` — the raw internal tool
 * name, so users read `Running mcp__openclaw__web_search…`. That derivation is
 * gone: ToolActivityTrace now narrates each step in place, in plain language,
 * where the work actually appears in the transcript.
 *
 * What is left is the bar's one honest job — covering the window where the
 * server says a run is active but this tab is seeing no stream events at all
 * (reconnect, a silent gap, a resumed session). In that window there is no
 * per-step truth to show, so it says only that work is in progress.
 */
export function StreamingStatusBar({ isStreaming, active }: StreamingStatusBarProps) {
  // While tokens or tool rows are actively arriving, the transcript itself is
  // the status — a second, vaguer indicator underneath it is noise.
  if (isStreaming || !active) return null;
  return (
    <div
      data-testid="streaming-status-bar"
      className="flex justify-center pointer-events-none px-4 pt-2 pb-1"
    >
      <div className="flex items-center gap-2 text-xs text-[var(--midnight-muted)] bg-[var(--cream-deep)]/80 backdrop-blur-sm rounded-full px-3 py-1.5 shadow-sm">
        <AgentGlyph state="thinking" size={16} />
        <span className="font-medium tracking-tight">Working…</span>
      </div>
    </div>
  );
}
