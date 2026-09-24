import type { RoomSummary } from "@lightbulb/shared";

/**
 * Slack's rule: the badge counts DMs and mentions, never plain channel
 * unreads, and never a muted conversation of either kind. A channel adds its
 * server-exact `mentionCount` (thread replies included), which the shared
 * room-list live patch keeps current between refreshes.
 */
export function badgeCount(rooms: readonly RoomSummary[]): number {
  return rooms.reduce((sum, room) => {
    if (room.muted) return sum;
    return sum + (room.kind === "channel" ? room.mentionCount ?? 0 : room.unreadCount);
  }, 0);
}
