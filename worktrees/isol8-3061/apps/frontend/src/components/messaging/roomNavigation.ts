import type { RoomSummary } from "@lightbulb/shared";

type NavigationDirection = 1 | -1;
type NavigableRoom = Pick<RoomSummary, "roomId" | "unreadCount" | "muted">;

/** A room the "next unread" jumps count: unread, and not muted. */
export const isUnreadTarget = (room: Pick<NavigableRoom, "unreadCount" | "muted">) =>
  !room.muted && room.unreadCount > 0;

/**
 * The room immediately before/after `currentId` in `rooms` (room order,
 * wrapping at both ends). `direction` is +1 for next, -1 for previous.
 * Returns null when there is nowhere to move (fewer than two rooms, or
 * `currentId` is the only room).
 */
export function adjacentRoomId(
  rooms: readonly NavigableRoom[],
  currentId: string | null,
  direction: NavigationDirection,
): string | null {
  if (rooms.length === 0) return null;
  const from = rooms.findIndex((room) => room.roomId === currentId);
  const index = (((from + direction) % rooms.length) + rooms.length) % rooms.length;
  const next = rooms[index];
  return next.roomId === currentId ? null : next.roomId;
}

/**
 * The next/previous unread room from `currentId`, wrapping at both ends and
 * considering `currentId` itself last. Generalizes RoomDirectory's
 * `selectNextUnread` wrapping search (forward-only there) to either
 * direction. Muted rooms are never a destination. Returns null when no room
 * is unread.
 */
export function nextUnreadRoomId(
  rooms: readonly NavigableRoom[],
  currentId: string | null,
  direction: NavigationDirection,
): string | null {
  const length = rooms.length;
  if (length === 0) return null;
  const from = rooms.findIndex((room) => room.roomId === currentId);
  for (let offset = 1; offset <= length; offset += 1) {
    const index = (((from + direction * offset) % length) + length) % length;
    if (isUnreadTarget(rooms[index])) return rooms[index].roomId;
  }
  return null;
}
