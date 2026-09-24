// apps/frontend/src/components/teams/issues/task-chat/task-chat-adapter.ts

// Ported from upstream Paperclip's task-chat comment adapter
// (paperclip/ui/src/components/task-chat/task-chat-adapter.ts @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). authorKind + the timestamp format are verbatim.
//
// Adaptations: our IssueComment (BFF-enriched authorName/authorClerkUserId)
// replaces IssueChatComment; there are no optimistic echoes (SWR revalidates
// after a post), session markers, cross-issue attribution chips or
// verification caveats in part A.

import type { CompanyAgent, IssueComment } from "@/components/teams/shared/types";
import type { TaskChatAuthorKind, TaskChatMessageItem } from "./task-chat-model";

export interface TaskChatAdapterContext {
  agentMap?: Map<string, CompanyAgent>;
  /** Clerk id of the viewer — marks their own comments (delete action). */
  currentUserId?: string | null;
}

function authorKind(comment: IssueComment): TaskChatAuthorKind {
  // The server-authored presentation contract wins over attribution.
  if (comment.presentation?.kind === "system_notice" || comment.authorType === "system") {
    return "system";
  }
  if (comment.authorAgentId) return "agent";
  if (comment.authorType === "user" || comment.authorKind === "user" || comment.authorUserId) {
    return "human";
  }
  return "agent";
}

/** Shared bubble-footer time format ("2:34 PM") — also used by the description bubble. */
export function formatTaskChatTimestamp(value: unknown): string | undefined {
  if (!value) return undefined;
  const d = value instanceof Date ? value : new Date(value as string);
  if (Number.isNaN(d.getTime())) return undefined;
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function commentToTaskChatItem(
  comment: IssueComment,
  ctx: TaskChatAdapterContext = {},
): TaskChatMessageItem | null {
  if (comment.deletedAt) return null;
  const kind = authorKind(comment);
  let authorName: string | undefined;
  let agentIcon: string | null | undefined;
  if (kind === "agent") {
    const agentId = comment.authorAgentId ?? null;
    authorName = (agentId && ctx.agentMap?.get(agentId)?.name) || "Agent";
    agentIcon = agentId ? ctx.agentMap?.get(agentId)?.icon : undefined;
  }
  const ownedByViewer =
    kind === "human" && Boolean(ctx.currentUserId) && comment.authorClerkUserId === ctx.currentUserId;
  if (kind === "human") {
    // Lightbulb: rooms are multi-human, so a human bubble keeps a byline
    // (stock's single-board UI has none) — "You", the BFF-enriched name, or "User".
    authorName = ownedByViewer ? "You" : comment.authorName || "User";
  }
  return {
    id: comment.id,
    renderKey: comment.id,
    kind: "message",
    author: kind,
    authorName,
    text: comment.body,
    timestamp: formatTaskChatTimestamp(comment.createdAt),
    agentIcon,
    ownedByViewer,
  };
}
