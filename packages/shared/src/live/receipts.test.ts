import { describe, expect, it } from "vitest";

import { parseLiveEvent } from "./types";

const R = "22222222-2222-4222-8222-222222222222";
const base = { schemaVersion: 1, eventId: "e", occurredAt: "2026-09-26T00:00:00Z", roomId: R };

describe("receipt events", () => {
  it("accept a human cursor, an agent read and a delivery", () => {
    expect(parseLiveEvent({ ...base, type: "room.read.changed", payload: { roomId: R, reader: { kind: "human", id: "u" }, lastReadSeq: 4 } })).not.toBeNull();
    expect(parseLiveEvent({ ...base, type: "room.read.changed", payload: { roomId: R, reader: { kind: "agent", id: "ceo" }, messageId: "m1" } })).not.toBeNull();
    expect(parseLiveEvent({ ...base, type: "room.delivery.changed", payload: { roomId: R, agentId: "ceo", lastDeliveredSeq: 4 } })).not.toBeNull();
  });
  it("accept a typing line with Lightbulb's stage label, reject an empty label", () => {
    const typing = { roomId: R, userId: "ceo", displayName: "Lightbulb", expiresAt: "2026-09-26T00:00:30Z" };
    expect(parseLiveEvent({ ...base, type: "room.typing.changed", payload: { ...typing, label: "Lightbulb is thinking…" } })).not.toBeNull();
    expect(parseLiveEvent({ ...base, type: "room.typing.changed", payload: { ...typing, label: "" } })).toBeNull();
  });
  it("reject a malformed one", () => {
    expect(parseLiveEvent({ ...base, type: "room.read.changed", payload: { roomId: R, reader: { kind: "human", id: "u" } } })).toBeNull();
    expect(parseLiveEvent({ ...base, type: "room.delivery.changed", payload: { roomId: R, agentId: "ceo", lastDeliveredSeq: -1 } })).toBeNull();
  });
});
