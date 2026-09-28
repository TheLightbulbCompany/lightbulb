import { expect, test } from "vitest";
import { parseLiveEvent } from "./types";

test("room.browser.frame carries a run's JPEG and its tab size; anything else is dropped", () => {
  const event = {
    schemaVersion: 1, eventId: "f-1", type: "room.browser.frame", occurredAt: "2026-09-28T02:00:00Z", roomId: "r",
    payload: { roomId: "r", agentId: "a", runId: "run", seq: 7, jpegBase64: "/9j/", w: 1280, h: 800 },
  };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, jpegBase64: "" } })).toBeNull();
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, w: "1280" } })).toBeNull();
  expect(parseLiveEvent({ ...event, roomId: "other" })).toBeNull();
});
