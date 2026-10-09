import { describe, expect, it } from "vitest";
import { parseLiveEvent } from "./types";
const base = { schemaVersion: 1, eventId: "e", type: "room.subagent.event", occurredAt: "t", roomId: "r" };
describe("room.subagent.event", () => {
  it("parses", () => {
    const payload = { roomId: "r", agentId: "a", events: [{ type: "subagent.start", seq: 1, payload: {} }] };
    expect(parseLiveEvent({ ...base, payload })?.type).toBe("room.subagent.event");
  });
  it("rejects non-array events", () => {
    expect(parseLiveEvent({ ...base, payload: { roomId: "r", agentId: "a", events: "x" } })).toBeNull();
  });
});
