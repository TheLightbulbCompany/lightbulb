import { expect, it } from "vitest";
import { parseLiveEvent } from "./types";

const event = { schemaVersion: 1, eventId: "e", type: "task.changed", occurredAt: "now",
  payload: { taskId: "t", part: "task", version: 3 } };

it("task.changed is known and validated", () => {
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: { taskId: "t", part: "task" } })).toBeNull();
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, version: "3" } })).toBeNull();
});

it("a status field rides along unread", () => {
  const withStatus = { ...event, payload: { ...event.payload, status: "working" } };
  expect(parseLiveEvent(withStatus)?.type).toBe("task.changed");
});
