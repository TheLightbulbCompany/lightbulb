// apps/frontend/src/components/teams/issues/task-chat/TaskChatInteractionCard.tsx

// Ported from upstream Paperclip's TaskChatInteractionCard
// (paperclip/ui/src/components/task-chat/TaskChatInteractionCard.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI) — the task-page presentation for durable issue
// interactions. Kept: in the timeline a PENDING interaction renders nothing (it
// lives in the composer takeover), a resolved one collapses to one receipt row;
// in the takeover the compact card renders the pending form.
//
// Dropped: the tool-action review row and the plan-review preview (our
// interaction types carry no `toolAction`, and TaskChatPlanPreviewCard is not
// ported), and shouldHideInteractionCard (degenerate / superseded-sibling
// filtering is not ported).

"use client";

import {
  TaskChatCompactInteractionCard,
  type TaskChatCompactInteractionCardProps,
} from "./TaskChatCompactInteractionCard";

export function TaskChatInteractionCard(props: TaskChatCompactInteractionCardProps) {
  const { interaction, presentation = "timeline" } = props;
  if (presentation === "timeline" && interaction.status === "pending") return null;
  return (
    <div id={`interaction-${interaction.id}`} className="w-full" data-testid="task-chat-interaction">
      <TaskChatCompactInteractionCard {...props} presentation={presentation} />
    </div>
  );
}
