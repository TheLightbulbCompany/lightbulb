// apps/frontend/src/components/teams/issues/task-chat/TaskChatBubble.tsx

// Ported from upstream Paperclip's TaskChatBubble
// (paperclip/ui/src/components/task-chat/TaskChatBubble.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Layout and footer logic are upstream's: human
// messages sit right in a bubble; agent messages sit on the page surface under
// an avatar author header; the timestamp line leads with the actions.
//
// Adaptations:
//   - --liveness-blue accent bubble  -> cream-deep bubble on the brand tokens
//     (our markdown links are midnight, unreadable on a solid accent)
//   - shadcn Avatar + AgentIcon      -> teams Identity (initials; no icon set here)
//   - MarkdownBody                   -> TaskChatMarkdown (mention chips)
//   - system notices                 -> a centered muted line (TaskChatSystemNotice
//     with its structured detail is part B), email cards, image gallery,
//     attachment chips, verification caveats and attached turns are not ported.

"use client";

import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Identity } from "@/components/teams/shared/components/Identity";
import { TaskChatMarkdown } from "./TaskChatMarkdown";
import type { TaskChatMessageItem } from "./task-chat-model";

interface TaskChatBubbleProps {
  item: TaskChatMessageItem;
  /** copy · 👍 · 👎 (· delete) controls for the footer line. */
  actions?: ReactNode;
}

export function TaskChatAgentIdentity({ agentName }: { agentName: string }) {
  return (
    <span className="flex items-center gap-2 px-1" data-testid="task-chat-agent-identity">
      <Identity name={agentName} size="sm" avatarOnly />
      <span className="text-sm font-semibold" style={{ color: "var(--midnight)" }}>
        {agentName}
      </span>
    </span>
  );
}

export function TaskChatBubble({ item, actions }: TaskChatBubbleProps) {
  if (item.author === "system") {
    return (
      <div
        className="w-full px-1 text-center text-xs"
        style={{ color: "var(--midnight-muted)" }}
        data-testid="task-chat-system-notice"
      >
        <TaskChatMarkdown>{item.text}</TaskChatMarkdown>
      </div>
    );
  }

  const isHuman = item.author === "human";
  const bodyText = item.text;
  return (
    <div
      id={`comment-${item.id}`}
      className={cn("flex w-full scroll-mt-4 flex-col gap-1", isHuman ? "items-end" : "items-start")}
    >
      {!isHuman && item.authorName ? (
        <TaskChatAgentIdentity agentName={item.authorName} />
      ) : null}
      {isHuman && item.authorName ? (
        <span className="px-1 text-xs" style={{ color: "var(--midnight-muted)" }}>
          {item.authorName}
        </span>
      ) : null}
      {bodyText.length > 0 ? (
        <div
          data-testid={isHuman ? "task-chat-human-bubble" : "task-chat-agent-bubble"}
          className={cn(
            "break-words py-2 text-sm",
            isHuman ? "max-w-[85%] rounded-2xl rounded-br-sm px-3.5" : "w-full bg-transparent px-1",
          )}
          style={
            isHuman
              ? { background: "var(--cream-deep)", color: "var(--midnight)", border: "1px solid var(--edge)" }
              : { color: "var(--midnight)" }
          }
        >
          <TaskChatMarkdown>{bodyText}</TaskChatMarkdown>
        </div>
      ) : null}
      {actions ? (
        <div className="flex items-center gap-1">
          {actions}
          {item.timestamp ? (
            <span className="px-1 text-2xs" style={{ color: "var(--midnight-muted)" }}>
              {item.timestamp}
            </span>
          ) : null}
        </div>
      ) : item.timestamp ? (
        <span className="px-1 text-2xs" style={{ color: "var(--midnight-muted)" }}>
          {item.timestamp}
        </span>
      ) : null}
    </div>
  );
}
