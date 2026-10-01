import { describe, expect, it } from "vitest";
import { parseLiveEvent } from "./types";
const base = { schemaVersion: 1, eventId: "e", type: "room.call.changed", occurredAt: "t", roomId: "r" };
describe("room.call.changed", () => {
  it("parses", () => { expect(parseLiveEvent({ ...base, payload: { roomId: "r", callId: "c", userId: "u", state: "started" } })?.type).toBe("room.call.changed"); });
  it("rejects a bad state", () => { expect(parseLiveEvent({ ...base, payload: { roomId: "r", callId: "c", userId: "u", state: "x" } })).toBeNull(); });
});
