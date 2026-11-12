import type { RoomSummary } from "@lightbulb/shared";

/** One of the member's own text chats with the assistant: a DM or group chat seating it and no other person.
 *  Anything else (a call or meeting room, a shared chat) opens read-only. */
export function isTextChat(room: Pick<RoomSummary, "kind" | "members"> | undefined, agentId?: string | null): boolean {
  return !!room && !!agentId && (room.kind === "dm" || room.kind === "group-dm")
    && room.members.some((m) => m.kind === "agent" && m.id === agentId)
    && room.members.filter((m) => m.kind === "human").length <= 1;
}
