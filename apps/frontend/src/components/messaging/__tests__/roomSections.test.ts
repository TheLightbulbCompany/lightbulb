import { describe, expect, it } from "vitest";
import { groupRooms } from "../roomSections";
import type { DirectoryRoom } from "../types";

const base: DirectoryRoom = {
  roomId: "r", kind: "channel", name: "Launch", roomRevision: 1, lastMessageSeq: 0,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null,
  starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, participants: [],
};
const room = (id: string, extra: Partial<DirectoryRoom> = {}): DirectoryRoom => ({ ...base, roomId: id, name: id, ...extra });
const nameOf = (value: DirectoryRoom) => value.name ?? value.roomId;

describe("groupRooms", () => {
  it("orders starred, custom sections, channels, group messages, then direct messages", () => {
    const groups = groupRooms(
      [room("beta"), room("alpha", { starred: true }), room("group", { kind: "group-dm", participants: [{ kind: "human", id: "a", displayName: "A", mentionHandle: null }] }), room("dm-1", { kind: "dm", participants: [{ kind: "human", id: "b", displayName: "B", mentionHandle: null }] }), room("shipping", { sectionId: "s-1" })],
      [{ id: "s-1", name: "Ship" }],
      { sort: "alpha", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Starred", "Ship", "Channels", "Group messages", "Direct messages"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["alpha"]);
    expect(groups[1].rooms.map((value) => value.roomId)).toEqual(["shipping"]);
    expect(groups[2].rooms.map((value) => value.roomId)).toEqual(["beta"]);
    expect(groups[3].rooms.map((value) => value.roomId)).toEqual(["group"]);
    expect(groups[4].rooms.map((value) => value.roomId)).toEqual(["dm-1"]);
  });

  it("separates shared channels into External connections using the canonical attribute", () => {
    const groups = groupRooms(
      [room("internal"), room("shared", { shared: true })],
      [],
      { sort: "alpha", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Channels", "External connections"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["internal"]);
    expect(groups[1].rooms.map((value) => value.roomId)).toEqual(["shared"]);
  });

  it("hides empty built-in channel groups for external-only and internal-only lists", () => {
    expect(groupRooms([room("shared", { shared: true })], [], { sort: "alpha", nameOf }).map((group) => group.label))
      .toEqual(["External connections"]);
    expect(groupRooms([room("internal")], [], { sort: "alpha", nameOf }).map((group) => group.label))
      .toEqual(["Channels"]);
  });

  it("separates group conversations from strict 1:1 direct messages", () => {
    const counterpart = { kind: "human" as const, id: "a", displayName: "A", mentionHandle: null };
    const groups = groupRooms([
      room("g", { kind: "group-dm", participants: [counterpart, { ...counterpart, id: "b", displayName: "B" }] }),
      room("d", { kind: "dm", participants: [counterpart] }),
      room("legacy-group", { kind: "dm", participants: [counterpart, { ...counterpart, id: "c", displayName: "C" }] }),
    ], [], { sort: "alpha", nameOf });
    expect(groups.map((group) => group.label)).toEqual(["Group messages", "Direct messages"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["g", "legacy-group"]);
    expect(groups[1].rooms.map((value) => value.roomId)).toEqual(["d"]);
  });

  it("suppresses unresolved direct-message ghosts until counterpart metadata resolves", () => {
    const unresolved = room("ghost", { kind: "dm", name: null, participants: [] });
    expect(groupRooms([unresolved], [], { sort: "alpha", nameOf })).toEqual([]);
    const resolved = { ...unresolved, participants: [{ kind: "agent" as const, id: "friday", displayName: "Friday", mentionHandle: null }] };
    expect(groupRooms([resolved], [], { sort: "alpha", nameOf })[0].rooms).toEqual([resolved]);
  });

  it("shows a room once — starred beats its section, which beats its kind", () => {
    const groups = groupRooms(
      [room("both", { starred: true, sectionId: "s-1" })],
      [{ id: "s-1", name: "Ship" }],
      { sort: "alpha", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Starred"]);
    expect(groups[0].rooms).toHaveLength(1);
  });

  it("drops empty groups and a section that no longer exists falls back to its kind", () => {
    const groups = groupRooms([room("orphan", { sectionId: "gone" })], [{ id: "s-1", name: "Ship" }], { sort: "alpha", nameOf });
    expect(groups.map((group) => group.label)).toEqual(["Channels"]);
  });

  it("sorts alphabetically by default and unread-first on request", () => {
    const rooms = [room("zulu", { unreadCount: 0 }), room("alpha", { unreadCount: 0 }), room("mike", { unreadCount: 3 })];
    expect(groupRooms(rooms, [], { sort: "alpha", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["alpha", "mike", "zulu"]);
    expect(groupRooms(rooms, [], { sort: "unread", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["mike", "alpha", "zulu"]);
  });

  it("never counts a muted room as unread for sorting", () => {
    const rooms = [room("alpha"), room("muted", { unreadCount: 9, muted: true })];
    expect(groupRooms(rooms, [], { sort: "unread", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["alpha", "muted"]);
  });
});
