import { describe, expect, it } from "vitest";
import { parseLiveEvent } from "./types";

const base = { schemaVersion: 1, eventId: "e1", type: "call.turn.frame", occurredAt: "2026-09-30T00:00:00Z" };

describe("call.turn.frame", () => {
  it("parses a call frame", () => {
    const event = parseLiveEvent({ ...base, payload: { callId: "c", turnId: "t", agentId: "a", frame: { type: "tool_start", tool: "browser" } } });
    expect(event?.type).toBe("call.turn.frame");
  });
  it("rejects a frame without a type", () => {
    expect(parseLiveEvent({ ...base, payload: { callId: "c", turnId: "t", agentId: "a", frame: {} } })).toBeNull();
  });
});
