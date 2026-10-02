import { describe, expect, it } from "vitest";
import { parseLiveEvent } from "./types";
const base = { schemaVersion: 1, eventId: "e", type: "room.huddle.changed", occurredAt: "t", roomId: "r" };
describe("room.huddle.changed", () => {
  it("parses", () => { expect(parseLiveEvent({ ...base, payload: { roomId: "r", userIds: ["u1", "u2"] } })?.type).toBe("room.huddle.changed"); });
  it("parses an empty huddle", () => { expect(parseLiveEvent({ ...base, payload: { roomId: "r", userIds: [] } })?.type).toBe("room.huddle.changed"); });
  it("rejects a missing userIds", () => { expect(parseLiveEvent({ ...base, payload: { roomId: "r" } })).toBeNull(); });
  it("rejects the retired orb marker room.call.changed", () => {
    expect(parseLiveEvent({ ...base, type: "room.call.changed", payload: { roomId: "r", callId: "c", userId: "u", state: "started" } })).toBeNull();
  });
  it("rejects a non-string user id", () => { expect(parseLiveEvent({ ...base, payload: { roomId: "r", userIds: ["u", 3] } })).toBeNull(); });
});
