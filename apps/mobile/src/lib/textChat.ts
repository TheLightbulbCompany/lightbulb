import type { RoomSummary } from "@lightbulb/shared";

/** One of the member's own text chats with the assistant: a DM or group chat with an agent and no other person
 *  (web's `textChats`, components/voice/data.ts). No agent-id check: the server seats the configured front agent
 *  (`messaging._lightbulb_only`), which need not be the CEO. Anything else (a call or meeting room, a chat shared
 *  with another person) opens read-only. */
export function isTextChat(room: Pick<RoomSummary, "kind" | "members"> | undefined): boolean {
  return !!room && (room.kind === "dm" || room.kind === "group-dm")
    && room.members.some((m) => m.kind === "agent")
    && room.members.filter((m) => m.kind === "human").length <= 1;
}
