import { afterEach, describe, expect, it, vi } from "vitest";
import { clearRoomsCache, roomsCache } from "../roomsCache";

const list = { rooms: [{ roomId: "r1" }], sections: [] } as never;
const message = { messageId: "m1" } as never;

afterEach(() => { vi.useRealTimers(); localStorage.clear(); });

describe("roomsCache", () => {
  it("keeps the list (never history) for its own scope only, and sign-out drops it", () => {
    vi.useFakeTimers();
    roomsCache.save("user_a:org_a", { rooms: list, history: [{ roomId: "r1", messages: [message], complete: true }] });
    expect(roomsCache.load("user_a:org_a")).toBeNull(); // written after the debounce
    vi.runAllTimers();
    expect(roomsCache.load("user_a:org_a")).toEqual({ rooms: list, history: [] });
    expect(localStorage.getItem("lightbulb.rooms:user_a:org_a")).not.toContain("m1");
    expect(roomsCache.load("user_b:org_a")).toBeNull();
    clearRoomsCache();
    expect(roomsCache.load("user_a:org_a")).toBeNull();
  });

  it("reads a corrupt or other-version snapshot as none", () => {
    localStorage.setItem("lightbulb.rooms:u:o", "{");
    expect(roomsCache.load("u:o")).toBeNull();
    localStorage.setItem("lightbulb.rooms:u:o", JSON.stringify({ version: 999, rooms: { rooms: [], sections: [] } }));
    expect(roomsCache.load("u:o")).toBeNull();
  });
});
