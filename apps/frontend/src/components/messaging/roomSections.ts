import type { LocalSession, RoomSection } from "@lightbulb/shared";
import type { DirectoryRoom } from "./types";

export type SidebarSort = "alpha" | "unread";
export interface RoomGroup {
  id: string;
  label: string;
  rooms: DirectoryRoom[];
  custom: boolean;
}

/**
 * The sidebar's groups, in display order: Starred · the owner's custom
 * sections · Channels · External connections · Direct messages (both `dm`
 * and `group-dm` — the spec has no separate "Group chats" section) · Archived
 * (an archived room lands there whatever else it is). Every room lands in exactly one
 * group, and empty groups are dropped unless named in `alwaysShow` (the
 * sidebar keeps Direct messages for the roster even before any DM exists).
 */
export function groupRooms(
  rooms: readonly DirectoryRoom[],
  sections: readonly RoomSection[],
  options: { sort: SidebarSort; nameOf: (room: DirectoryRoom) => string; alwaysShow?: readonly string[] },
): RoomGroup[] {
  const known = new Set(sections.map((section) => section.id));
  const bucket = (room: DirectoryRoom) => room.archivedAt ? "archived" : room.starred ? "starred"
    : room.sectionId && known.has(room.sectionId) ? room.sectionId
      : room.kind === "channel" && room.shared ? "external"
        : room.kind === "channel" ? "channels" : "direct";
  const order: RoomGroup[] = [
    { id: "starred", label: "Starred", rooms: [], custom: false },
    ...sections.map((section) => ({ id: section.id, label: section.name, rooms: [], custom: true })),
    { id: "channels", label: "Channels", rooms: [], custom: false },
    { id: "external", label: "External connections", rooms: [], custom: false },
    { id: "direct", label: "Direct messages", rooms: [], custom: false },
    { id: "archived", label: "Archived", rooms: [], custom: false },
  ];
  const byId = new Map(order.map((group) => [group.id, group]));
  for (const room of rooms) byId.get(bucket(room))?.rooms.push(room);
  // A muted room never carries a badge, so it never jumps the queue either.
  const unread = (room: DirectoryRoom) => room.muted ? 0 : room.unreadCount;
  for (const group of order) {
    group.rooms.sort((left, right) => (options.sort === "unread" && unread(right) !== unread(left)
      ? unread(right) - unread(left)
      : options.nameOf(left).localeCompare(options.nameOf(right))));
  }
  return order.filter((group) => group.rooms.length > 0 || options.alwaysShow?.includes(group.id));
}

export interface SidebarSession { session: LocalSession; ownerName: string; own: boolean }
