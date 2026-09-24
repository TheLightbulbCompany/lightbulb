// apps/frontend/src/components/teams/issues/task-chat/task-chat-model.ts

// Ported from upstream Paperclip's task-chat presentation model
// (paperclip/ui/src/components/task-chat/task-chat-model.ts @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Field docs are verbatim.
//
// Part A subset: the message item — the only kind the comment stream produces.
// Thinking, tool, diff, usage, plan, protocol, runtime-request and turn items
// belong to surfaces not ported yet (see TaskChatThread's header).

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
  timestamp?: string;
  /** Lightbulb: the viewer authored this comment (drives the delete action). */
  ownedByViewer?: boolean;
  /** Lightbulb: no author id at all — labelled "Comment", never voted on. */
  unattributed?: boolean;
}
