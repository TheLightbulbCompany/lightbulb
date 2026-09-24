import { ROOMS_SNAPSHOT_VERSION, type RoomsSnapshot } from "@lightbulb/shared";
import { AppState } from "react-native";

import { clearRoomsCache, roomsCache } from "../roomsCache";

// The jest-setup stand-in for expo-file-system keeps written text in memory
// (`__files`, keyed by uri), so these read back exactly what reached "disk".
type FakeFs = { __files: Map<string, string> };
const files = () => (require("expo-file-system") as unknown as FakeFs).__files;
const cacheFiles = () => [...files().keys()].filter((uri) => uri.includes("/rooms-cache/"));

const snapshot = (roomId: string): RoomsSnapshot => ({
  rooms: { rooms: [{ roomId } as never], sections: [] },
  history: [{ roomId, messages: [], complete: true }],
});
const A = "user_a:org_a";

beforeEach(() => {
  jest.useFakeTimers();
  clearRoomsCache();
  files().clear();
});
afterEach(() => jest.useRealTimers());

describe("roomsCache", () => {
  it("writes 2 s after a change, not on every change", () => {
    roomsCache.save(A, snapshot("room-1"));
    roomsCache.save(A, snapshot("room-2"));
    expect(cacheFiles()).toHaveLength(0);
    jest.advanceTimersByTime(2_000);
    expect(roomsCache.load(A)?.rooms.rooms[0].roomId).toBe("room-2");
  });

  it("writes at once when the app leaves the foreground", () => {
    roomsCache.save(A, snapshot("room-1"));
    const listener = (AppState.addEventListener as jest.Mock).mock.calls.at(-1)?.[1];
    listener("background");
    expect(roomsCache.load(A)?.rooms.rooms[0].roomId).toBe("room-1");
  });

  it("keeps one file per user and workspace, and reads only the matching one", () => {
    roomsCache.save(A, snapshot("room-a"));
    jest.advanceTimersByTime(2_000);
    expect(roomsCache.load("user_b:org_a")).toBeNull();
    expect(roomsCache.load("user_a:org_b")).toBeNull();
    expect(roomsCache.load(A)?.rooms.rooms[0].roomId).toBe("room-a");
  });

  it("refuses a scope that is not a plain id pair", () => {
    roomsCache.save("../escape:org_a", snapshot("room-1"));
    jest.advanceTimersByTime(2_000);
    expect(cacheFiles()).toHaveLength(0);
    expect(roomsCache.load("../escape:org_a")).toBeNull();
  });

  it("discards a snapshot of another version", () => {
    roomsCache.save(A, snapshot("room-1"));
    jest.advanceTimersByTime(2_000);
    const [uri] = cacheFiles();
    files().set(uri, JSON.stringify({ ...JSON.parse(files().get(uri)!), version: ROOMS_SNAPSHOT_VERSION + 1 }));
    expect(roomsCache.load(A)).toBeNull();
    expect(cacheFiles()).toHaveLength(0);
  });

  it("discards a corrupt or malformed file silently", () => {
    roomsCache.save(A, snapshot("room-1"));
    jest.advanceTimersByTime(2_000);
    const [uri] = cacheFiles();
    files().set(uri, "{not json");
    expect(roomsCache.load(A)).toBeNull();
    files().set(uri, JSON.stringify({ version: ROOMS_SNAPSHOT_VERSION, rooms: null, history: [] }));
    expect(roomsCache.load(A)).toBeNull();
  });

  it("sign-out deletes every scope's file and drops a pending write", () => {
    roomsCache.save(A, snapshot("room-a"));
    roomsCache.save("user_b:org_b", snapshot("room-b"));
    jest.advanceTimersByTime(2_000);
    roomsCache.save(A, snapshot("room-a2")); // still pending when the user signs out
    expect(cacheFiles()).toHaveLength(2);
    clearRoomsCache();
    jest.advanceTimersByTime(2_000);
    expect(cacheFiles()).toHaveLength(0);
    expect(roomsCache.load(A)).toBeNull();
  });
});
