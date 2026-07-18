import type { ToolPillStatus } from "./ToolPill";

/**
 * One rendered timeline entry on the agent chat screen. Extracted from
 * `app/(app)/agent/[agentId].tsx` so the history projection
 * (`lib/chatHistory.ts`) can target the same shape the live stream builds.
 */
export interface ChatItem {
  id: string;
  role: "user" | "assistant" | "tool" | "error";
  text: string;
  streaming?: boolean;
  /**
   * Error items only: typed code from the backend error frame (gate codes
   * like `trial_daily_cap` / `trial_expired`, or OpenClaw errorKinds).
   * Drives the structured ChatErrorCard; absent → plain error card.
   */
  code?: string;
  /** Tool items only: tool name + input args from the tool_start frame. */
  tool?: string;
  args?: Record<string, unknown>;
  /** Tool items only: result text attached when tool_end/tool_error lands. */
  result?: string;
  toolStatus?: ToolPillStatus;
  /**
   * The run this item belongs to. Live items are stamped so a history
   * refetch can prune runs the seeded transcript now covers (dedupe rule,
   * sessions contract §5); client-local errors carry none and survive.
   */
  runId?: string;
}
