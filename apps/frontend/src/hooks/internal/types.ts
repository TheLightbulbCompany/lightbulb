// apps/frontend/src/hooks/internal/types.ts
//
// Shared types used by useAgentChat and its helper modules. Mirrors
// the upstream OpenClaw control-ui shape (~/Desktop/openclaw/ui/src/ui)
// with Isol8-specific additions (live thinking) called out inline.

/** A content block within a HistoryMessage. Field shapes vary by `type`. */
export interface ContentBlock {
  type: string;
  // text-flavored
  text?: string;
  // thinking-flavored
  thinking?: string;
  // tool_use-flavored
  id?: string;
  name?: string;
  args?: unknown;
  arguments?: unknown;
  input?: unknown;
  // tool_result-flavored
  tool_use_id?: string;
  toolUseId?: string;
  toolCallId?: string;
  content?: unknown;
  result?: unknown;
  isError?: boolean;
  // image-flavored (after upstream's chat-display-projection)
  omitted?: boolean;
  bytes?: number;
}

/** A whole message from chat.history or synthesized at chat.final. */
export interface HistoryMessage {
  role: string;
  content?: ContentBlock[];
  text?: string;
  timestamp?: number;
  runId?: string;
  toolCallId?: string;
  tool_call_id?: string;
  toolName?: string;
  tool_name?: string;
  errorCode?: string;
  __openclaw?: { kind?: string };
}

/** Live tool tracking entry (private to useAgentChat). */
export interface ToolStreamEntry {
  toolCallId: string;
  runId: string;
  name: string;
  args?: unknown;
  output?: unknown;
  status: "running" | "done" | "error";
  startedAt: number;
  updatedAt: number;
}

/** A single render item produced by buildChatItems(). */
export type ChatRenderItem =
  | { kind: "message"; key: string; message: HistoryMessage }
  | { kind: "stream-segment"; key: string; text: string; ts: number }
  | { kind: "stream"; key: string; text: string; runId: string }
  | { kind: "live-thinking"; key: string; text: string; runId: string }
  | { kind: "divider"; key: string; label: string };

/** Visual side of a rendered group. */
export type RenderSide = "user" | "assistant";

/** A visual bubble group — consecutive items on the same side. */
export interface RenderGroup {
  side: RenderSide;
  key: string;
  items: ChatRenderItem[];
}
