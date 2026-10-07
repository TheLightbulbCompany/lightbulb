import { describe, expect, it } from "vitest";
import { adjacentRoomId, nextUnreadRoomId } from "../roomNavigation";

const rooms = [
  { roomId: "a", unreadCount: 0, muted: false },
  { roomId: "b", unreadCount: 2, muted: false },
  { roomId: "c", unreadCount: 0, muted: false },
];

describe("adjacentRoomId", () => {
  it("moves to the next room", () => {
    expect(adjacentRoomId(rooms, "a", 1)).toBe("b");
  });
  it("moves to the previous room, wrapping to the end", () => {
    expect(adjacentRoomId(rooms, "a", -1)).toBe("c");
  });
  it("wraps forward from the last room to the first", () => {
    expect(adjacentRoomId(rooms, "c", 1)).toBe("a");
  });
  it("is a no-op with a single room", () => {
    expect(adjacentRoomId([rooms[0]], "a", 1)).toBeNull();
    expect(adjacentRoomId([rooms[0]], "a", -1)).toBeNull();
  });
  it("is a no-op with no rooms", () => {
    expect(adjacentRoomId([], null, 1)).toBeNull();
  });
  it("treats an unknown current room as unselected, starting from before index 0", () => {
    expect(adjacentRoomId(rooms, "not-a-room", 1)).toBe("a");
    expect(adjacentRoomId(rooms, "not-a-room", -1)).toBe("b");
  });
});

describe("nextUnreadRoomId", () => {
  it("finds the next unread room, wrapping past the end", () => {
    expect(nextUnreadRoomId(rooms, "c", 1)).toBe("b");
  });
  it("finds the previous unread room, wrapping past the start", () => {
    expect(nextUnreadRoomId(rooms, "a", -1)).toBe("b");
  });
  it("returns null when nothing is unread", () => {
    const readRooms = rooms.map((room) => ({ ...room, unreadCount: 0 }));
    expect(nextUnreadRoomId(readRooms, "a", 1)).toBeNull();
  });
  it("skips a muted room even when it is unread", () => {
    const muted = rooms.map((room) => room.roomId === "b" ? { ...room, muted: true } : room);
    expect(nextUnreadRoomId(muted, "c", 1)).toBeNull();
    expect(nextUnreadRoomId([...muted, { roomId: "d", unreadCount: 1, muted: false }], "c", 1)).toBe("d");
  });
  it("matches RoomDirectory's forward wrapping search from an unknown current room", () => {
    // currentId not found (from = -1): search starts at index 0, exactly
    // mirroring rooms.slice(0).concat(rooms.slice(0, 0)) in RoomDirectory.
    expect(nextUnreadRoomId(rooms, "not-a-room", 1)).toBe("b");
  });
});
