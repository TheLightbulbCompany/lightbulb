import { expect, test } from "vitest";
import { parseLiveEvent } from "./types";

test("workspace session hints carry only humanUserId", () => {
  const event = { schemaVersion: 1, eventId: "hint-1", type: "workspace.sessions.changed",
    occurredAt: "2026-09-11T00:00:00Z", payload: { humanUserId: "u" } };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, extra: 1 } })).toBeNull();
});
