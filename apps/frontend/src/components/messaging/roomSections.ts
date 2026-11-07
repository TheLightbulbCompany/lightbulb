import type { LocalSession, RoomSection, SessionRow } from "@lightbulb/shared";
import type { DirectoryRoom, RoomParticipant } from "./types";

export type SidebarSort = "recent" | "name" | "unread";
export interface RoomGroup {
  id: string;
  label: string;
  rooms: DirectoryRoom[];
  custom: boolean;
}

/**
 * The sidebar's groups, in display order: Starred · the owner's custom
 * sections · Team (both `dm` and `group-dm` — there is no separate "Group
 * chats" section) · Channels · External connections · Archived (an archived
 * room lands there whatever else it is). Every room lands in exactly one
 * group, and empty groups are dropped unless named in `alwaysShow` (the
 * sidebar keeps Team for the roster even before any DM exists). The sidebar
 * shows Sessions between Channels and External connections.
 */
export function groupRooms(
  rooms: readonly DirectoryRoom[],
  sections: readonly RoomSection[],
  options: {
    sort: SidebarSort; nameOf: (room: DirectoryRoom) => string; alwaysShow?: readonly string[];
    /** The one front agent (`@lightbulb`). A DM with any other agent is a specialist's: it leaves the
     *  sidebar (one voice) and is reached from the Team page, where specialists are hired and managed. It stays
     *  under Team while it has unread messages or is the open conversation, so nothing new hides. */
    frontAgentId?: string | null;
    currentRoomId?: string | null;
  },
): RoomGroup[] {
  const known = new Set(sections.map((section) => section.id));
  const specialistDm = (room: DirectoryRoom) => options.frontAgentId !== undefined && room.kind === "dm"
    && room.participants.length === 1 && room.participants[0].kind === "agent" && room.participants[0].id !== options.frontAgentId;
  const bucket = (room: DirectoryRoom) => room.archivedAt ? "archived" : room.starred ? "starred"
    : room.sectionId && known.has(room.sectionId) ? room.sectionId
      : room.kind === "channel" && room.shared ? "external"
        : room.kind === "channel" ? "channels"
          : specialistDm(room) && room.unreadCount === 0 && room.roomId !== options.currentRoomId ? null : "direct";
  const order: RoomGroup[] = [
    // UX slice 6: the starred group reads "Pinned", like the row's Pin.
    { id: "starred", label: "Pinned", rooms: [], custom: false },
    ...sections.map((section) => ({ id: section.id, label: section.name, rooms: [], custom: true })),
    { id: "direct", label: "Chats", rooms: [], custom: false },
    { id: "channels", label: "Channels", rooms: [], custom: false },
    { id: "external", label: "External connections", rooms: [], custom: false },
    { id: "archived", label: "Archived", rooms: [], custom: false },
  ];
  const byId = new Map(order.map((group) => [group.id, group]));
  for (const room of rooms) {
    const id = bucket(room);
    if (id) byId.get(id)?.rooms.push(room);
  }
  // A muted room never carries a badge, so it never jumps the queue either.
  const unread = (room: DirectoryRoom) => room.muted ? 0 : room.unreadCount;
  // Newest message first; a room nobody has written in yet sorts after every one that has.
  const latest = (room: DirectoryRoom) => room.lastMessageAt ? Date.parse(room.lastMessageAt) : -Infinity;
  for (const group of order) {
    group.rooms.sort((left, right) => (options.sort === "unread" && unread(right) !== unread(left)
      ? unread(right) - unread(left)
      : options.sort === "recent" && latest(right) !== latest(left) ? latest(right) - latest(left)
        : options.nameOf(left).localeCompare(options.nameOf(right))));
  }
  return order.filter((group) => group.rooms.length > 0 || options.alwaysShow?.includes(group.id));
}

/** `unread`: new output since this viewer last looked (`finished` once it stopped working). */
export interface SidebarSession { session: LocalSession; ownerName: string; own: boolean; owner: RoomParticipant; unread?: "unread" | "finished" | null }

/** One row of the sidebar's one session list: `SessionRow` plus its owner's participant and unread mark. */
export interface SidebarRow { row: SessionRow; owner: RoomParticipant; unread?: "unread" | "finished" | null }
