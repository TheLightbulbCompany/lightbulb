// apps/frontend/src/components/teams/issues/lib/teams-thread-items.ts

// Small merge that interleaves issue comments and agent-initiated thread
// interactions into one chronologically sorted timeline. The sort mirrors
// upstream Paperclip's `sortByCreated` + ordered-message tie-break
// (paperclip/ui/src/lib/issue-chat-messages.ts): order by
// creation timestamp ascending, then by id for a stable result when two items
// share a timestamp. We deliberately do NOT port the assistant-ui /
// run-transcript machinery — comments + interactions are the only two streams
// the Teams conversation interleaves.

import type {
  IssueComment,
  IssueThreadInteraction,
} from "@/components/teams/shared/types";

export type TeamsThreadItem =
  | { kind: "comment"; id: string; createdAt: string; comment: IssueComment }
  | {
      kind: "interaction";
      id: string;
      createdAt: Date | string;
      interaction: IssueThreadInteraction;
    };

function toTimestamp(value: Date | string | null | undefined): number {
  if (!value) return 0;
  const date = value instanceof Date ? value : new Date(value);
  const ms = date.getTime();
  return Number.isFinite(ms) ? ms : 0;
}

export function buildTeamsThreadItems({
  comments,
  interactions,
}: {
  comments: readonly IssueComment[];
  interactions: readonly IssueThreadInteraction[];
}): TeamsThreadItem[] {
  const items: TeamsThreadItem[] = [
    ...comments.map(
      (comment): TeamsThreadItem => ({
        kind: "comment",
        id: comment.id,
        createdAt: comment.createdAt,
        comment,
      }),
    ),
    ...interactions.map(
      (interaction): TeamsThreadItem => ({
        kind: "interaction",
        id: interaction.id,
        createdAt: interaction.createdAt,
        interaction,
      }),
    ),
  ];

  return items.sort((a, b) => {
    const diff = toTimestamp(a.createdAt) - toTimestamp(b.createdAt);
    if (diff !== 0) return diff;
    return a.id.localeCompare(b.id);
  });
}
