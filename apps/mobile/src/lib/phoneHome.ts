import type { RoomSummary } from "@lightbulb/shared/messaging";

export function findCeoDm(
  rooms: RoomSummary[],
  currentUserId?: string | null,
  ceoAgentId?: string | null,
): RoomSummary | null {
  if (!ceoAgentId) return null;
  return rooms.find((room) => room.kind === "dm" && room.members.some((member) =>
    member.id !== currentUserId &&
    member.id === ceoAgentId,
  )) ?? null;
}