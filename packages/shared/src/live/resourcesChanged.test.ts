import { expect, test } from "vitest";
import { parseLiveEvent } from "./types";

test("room resource hints are registered and cannot carry transcript content", () => {
  const event = { schemaVersion: 1, eventId: "hint-1", type: "room.resources.changed",
    occurredAt: "2026-09-11T00:00:00Z", roomId: "room-1", payload: { roomId: "room-1" } };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, text: "private" } })).toBeNull();
  expect(parseLiveEvent({ ...event, payload: { roomId: "other" } })).toBeNull();
});
