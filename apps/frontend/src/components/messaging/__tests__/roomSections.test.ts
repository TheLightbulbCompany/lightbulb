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
  it("orders starred, custom sections, channels, then direct messages", () => {
    const groups = groupRooms(
      [room("beta"), room("alpha", { starred: true }), room("dm-1", { kind: "dm" }), room("shipping", { sectionId: "s-1" })],
      [{ id: "s-1", name: "Ship" }],
      { sort: "alpha", nameOf },
    );
    expect(groups.map((group) => group.label)).toEqual(["Starred", "Ship", "Channels", "Direct messages"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["alpha"]);
    expect(groups[1].rooms.map((value) => value.roomId)).toEqual(["shipping"]);
    expect(groups[2].rooms.map((value) => value.roomId)).toEqual(["beta"]);
    expect(groups[3].rooms.map((value) => value.roomId)).toEqual(["dm-1"]);
  });

  it("keeps group DMs inside Direct messages", () => {
    const groups = groupRooms([room("g", { kind: "group-dm" }), room("d", { kind: "dm" })], [], { sort: "alpha", nameOf });
    expect(groups.map((group) => group.label)).toEqual(["Direct messages"]);
    expect(groups[0].rooms.map((value) => value.roomId)).toEqual(["d", "g"]);
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

import { orderSidebarSessions, type SidebarSession } from "../roomSections";

const s = (over: Partial<SidebarSession["session"]>, ownerName: string, own = false): SidebarSession => ({
  own, ownerName,
  session: { sessionId: "x", deviceId: "d", humanUserId: "h", provider: "claude", providerVersion: "2", generation: "g", status: "idle", activeTurnId: null,
    capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, ...over },
});

it("orders sidebar sessions: mine first, then people by name, busy before waiting before idle, then most recent", () => {
  const rows = [
    s({ sessionId: "b-idle", lastEventAt: "2026-09-14T10:00:00Z" }, "Bob"),
    s({ sessionId: "a-idle-old", lastEventAt: "2026-09-14T09:00:00Z" }, "Alice"),
    s({ sessionId: "a-idle-new", lastEventAt: "2026-09-14T11:00:00Z" }, "Alice"),
    s({ sessionId: "a-busy", status: "busy", lastEventAt: "2026-09-14T08:00:00Z" }, "Alice"),
    s({ sessionId: "a-wait", status: "waiting-approval" }, "Alice"),
    s({ sessionId: "me", lastEventAt: "2026-09-14T01:00:00Z" }, "Me", true),
  ];
  expect(orderSidebarSessions(rows).map((r) => r.session.sessionId)).toEqual(["me", "a-busy", "a-wait", "a-idle-new", "a-idle-old", "b-idle"]);
});
