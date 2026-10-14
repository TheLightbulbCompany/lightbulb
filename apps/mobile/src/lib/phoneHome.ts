import type { RoomSummary } from "@lightbulb/shared/messaging";

const CEO_NAMES = new Set(["ceo", "friday"]);

export function findCeoDm(rooms: RoomSummary[], currentUserId?: string | null): RoomSummary | null {
  return rooms.find((room) => room.kind === "dm" && room.members.some((member) =>
    member.id !== currentUserId &&
    (member.id.toLowerCase() === "ceo" || CEO_NAMES.has(member.displayName.trim().toLowerCase())),
  )) ?? null;
}