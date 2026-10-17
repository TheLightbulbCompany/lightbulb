import type { RoomSummary } from "@lightbulb/shared";

/** Per-room card policy, the web's `cardControls`: the feed shows fences as code; an archived or unknown room shows cards read-only. */
export function cardControls(room: Pick<RoomSummary, "feed" | "archivedAt"> | undefined): { cardsEnabled: boolean; repliesEnabled: boolean } {
  const feed = Boolean(room?.feed);
  return { cardsEnabled: !feed, repliesEnabled: room !== undefined && !feed && !room.archivedAt };
}
