// apps/frontend/src/components/teams/issues/task-chat/task-chat-model.ts

// Ported from upstream Paperclip's task-chat presentation model
// (paperclip/ui/src/components/task-chat/task-chat-model.ts @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Field docs are verbatim.
//
// Part A subset: the message and thinking items — the only two kinds the
// comment stream and the hermes_gateway transcript produce. Tool, diff, usage,
// plan, protocol, runtime-request and turn items belong to adapters/surfaces we
// have not ported (see TaskChatThread's header for the part B list).

/** Who authored a thread row — the primary legibility signal. */
export type TaskChatAuthorKind = "human" | "agent" | "system";

/** A human/agent/system message bubble. */
export interface TaskChatMessageItem {
  /** Stable UI identity through optimistic acknowledgement; id remains canonical. */
  renderKey?: string;
  id: string;
  kind: "message";
  author: TaskChatAuthorKind;
  authorName?: string;
  text: string;
  /** Runner-authored output channel. Legacy adapters leave this unset. */
  channel?: "progress" | "final" | "unknown";
  timestamp?: string;
  /** Show a streaming cursor and suppress collapse while true. */
  streaming?: boolean;
  /** Assigned agent icon name (AgentIconName) for the avatar header. */
  agentIcon?: string | null;
  /** A transient provider progress update, distinct from the durable final response. */
  interstitial?: boolean;
  /** Epoch ms of the message's first streamed chunk. */
  atMs?: number;
  /** Index of the latest transcript event folded into this logical block. */
  transcriptIndex?: number;
  /** Lightbulb: the viewer authored this comment (drives the delete action). */
  ownedByViewer?: boolean;
}

/** Collapsed chain-of-thought (ACP agent_thought_chunk). */
export interface TaskChatThinkingItem {
  id: string;
  kind: "thinking";
  lines: string[];
  streaming?: boolean;
  /** When true, render collapsed ("Worked for N") with an expand affordance. */
  collapsed?: boolean;
  /** Human-readable elapsed label for the collapsed header. */
  summaryLabel?: string;
  /** Provider-emitted reasoning surface; never synthesized by the UI. */
  channel?: "summary" | "detail" | "unknown";
  /** A real provider reasoning lifecycle with no provider-authored text. */
  lifecycleOnly?: boolean;
  /** Index of the latest transcript event folded into this logical block. */
  transcriptIndex?: number;
}

export type TaskChatItem = TaskChatMessageItem | TaskChatThinkingItem;
