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
  it("sorts by latest message, newest first, with never-written rooms last and starred still pinned", () => {
    const at = (iso: string | null) => ({ lastMessageAt: iso });
    const groups = groupRooms([
      room("old", at("2026-09-24T09:00:00Z")),
      room("quiet", at(null)),
      room("new", at("2026-09-24T10:00:00Z")),
      room("pinned", { ...at("2026-09-01T00:00:00Z"), starred: true }),
    ], [], { sort: "recent", nameOf });
    expect(groups.map((group) => [group.id, group.rooms.map((value) => value.roomId)])).toEqual([
      ["starred", ["pinned"]],
      ["channels", ["new", "old", "quiet"]],
    ]);
  });

  it("orders starred, custom sections, Team, channels, external connections, then archived", () => {
    const groups = groupRooms(
      [room("beta"), room("alpha", { starred: true }), room("dm-1", { kind: "dm" }), room("shipping", { sectionId: "s-1" }),
        room("ext", { shared: true }), room("old", { archivedAt: "2026-09-23T00:00:00Z" })],
      [{ id: "s-1", name: "Ship" }],
      { sort: "name", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Starred", "Ship", "People", "Channels", "External connections", "Archived"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["alpha"]);
    expect(groups[1].rooms.map((value) => value.roomId)).toEqual(["shipping"]);
    expect(groups[2].rooms.map((value) => value.roomId)).toEqual(["dm-1"]);
    expect(groups[3].rooms.map((value) => value.roomId)).toEqual(["beta"]);
  });

  it("separates shared channels into External connections using the canonical attribute", () => {
    const groups = groupRooms(
      [room("internal"), room("shared", { shared: true })],
      [],
      { sort: "name", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Channels", "External connections"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["internal"]);
    expect(groups[1].rooms.map((value) => value.roomId)).toEqual(["shared"]);
  });

  it("hides empty built-in channel groups for external-only and internal-only lists", () => {
    expect(groupRooms([room("shared", { shared: true })], [], { sort: "name", nameOf }).map((group) => group.label))
      .toEqual(["External connections"]);
    expect(groupRooms([room("internal")], [], { sort: "name", nameOf }).map((group) => group.label))
      .toEqual(["Channels"]);
  });

  it("keeps group DMs inside Team", () => {
    const groups = groupRooms([room("g", { kind: "group-dm" }), room("d", { kind: "dm" })], [], { sort: "name", nameOf });
    expect(groups.map((group) => group.label)).toEqual(["People"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["d", "g"]);
  });

  it("shows a room once — starred beats its section, which beats its kind", () => {
    const groups = groupRooms(
      [room("both", { starred: true, sectionId: "s-1" })],
      [{ id: "s-1", name: "Ship" }],
      { sort: "name", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Starred"]);
    expect(groups[0].rooms).toHaveLength(1);
  });

  it("drops empty groups and a section that no longer exists falls back to its kind", () => {
    const groups = groupRooms([room("orphan", { sectionId: "gone" })], [{ id: "s-1", name: "Ship" }], { sort: "name", nameOf });
    expect(groups.map((group) => group.label)).toEqual(["Channels"]);
  });

  it("sorts alphabetically by default and unread-first on request", () => {
    const rooms = [room("zulu", { unreadCount: 0 }), room("alpha", { unreadCount: 0 }), room("mike", { unreadCount: 3 })];
    expect(groupRooms(rooms, [], { sort: "name", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["alpha", "mike", "zulu"]);
    expect(groupRooms(rooms, [], { sort: "unread", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["mike", "alpha", "zulu"]);
  });

  it("never counts a muted room as unread for sorting", () => {
    const rooms = [room("alpha"), room("muted", { unreadCount: 9, muted: true })];
    expect(groupRooms(rooms, [], { sort: "unread", nameOf })[0].rooms.map((value) => value.roomId)).toEqual(["alpha", "muted"]);
  });
});

describe("archived rooms", () => {
  it("leave starred, custom and built-in groups for one Archived group at the bottom", () => {
    const archivedAt = "2026-09-23T00:00:00Z";
    const groups = groupRooms(
      [room("live"), room("old", { archivedAt }), room("fav", { archivedAt, starred: true }), room("kept", { archivedAt, sectionId: "s-1" })],
      [{ id: "s-1", name: "Ship" }],
      { sort: "name", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Channels", "Archived"]);
    expect(groups[1].rooms.map((value) => value.roomId)).toEqual(["fav", "kept", "old"]);
  });

  it("move an archived group chat out of Team", () => {
    const groups = groupRooms(
      [room("dm-1", { kind: "dm" }), room("trio", { kind: "group-dm", archivedAt: "2026-09-23T00:00:00Z" })],
      [],
      { sort: "name", nameOf },
    );
    expect(groups.map((group) => [group.label, group.rooms.map((value) => value.roomId)]))
      .toEqual([["People", ["dm-1"]], ["Archived", ["trio"]]]);
  });
});
