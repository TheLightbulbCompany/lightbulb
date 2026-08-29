// apps/frontend/src/hooks/internal/types.ts
//
// Web-only render types for useAgentChat and its helper modules.
//
// The WIRE types (ContentBlock, HistoryMessage) moved to @lightbulb/shared
// (packages/shared/src/chat/history.ts) — mobile had its own copy of them and
// the pair had drifted. Re-exported here so existing imports are unaffected.

export type { ContentBlock, HistoryMessage } from "@lightbulb/shared";
import type { HistoryMessage } from "@lightbulb/shared";

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
  /** Live incremental output (tool phase:update / command_output). Rendered in
   *  the tool pill while running; superseded by `output` on terminal. */
  progressText?: string;
  /** Non-tool activity surfaces (plan/patch/item/acp) reuse this entry shape
   *  via a synthetic id; `kind` distinguishes them from real tools. */
  kind?: "tool" | "activity";
  /** Human label for an activity entry's pill. */
  title?: string;
}

/** A single render item produced by buildChatItems(). */
export type ChatRenderItem =
  | { kind: "message"; key: string; message: HistoryMessage }
  | { kind: "stream-segment"; key: string; text: string; ts: number }
  | { kind: "stream"; key: string; text: string; runId: string }
  | { kind: "live-thinking"; key: string; text: string; runId: string }
  | { kind: "divider"; key: string; label: string }
  // A `/btw` (detached side-channel) answer. Ephemeral — held in transient
  // state, never folded into the transcript/history, mirroring OpenClaw's
  // "Not saved to chat history" side card.
  | { kind: "btw"; key: string; question: string; text: string; isError?: boolean };

/** Visual side of a rendered group. */
export type RenderSide = "user" | "assistant" | "btw";

/** A visual bubble group — consecutive items on the same side. */
export interface RenderGroup {
  side: RenderSide;
  key: string;
  items: ChatRenderItem[];
}
