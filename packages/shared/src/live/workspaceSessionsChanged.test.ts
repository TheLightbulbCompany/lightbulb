import { expect, test } from "vitest";
import { parseLiveEvent } from "./types";

test("workspace session hints carry only humanUserId", () => {
  const event = { schemaVersion: 1, eventId: "hint-1", type: "workspace.sessions.changed",
    occurredAt: "2026-09-11T00:00:00Z", payload: { humanUserId: "u" } };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, extra: 1 } })).toBeNull();
  // A streaming batch marks itself; anything but `true` is malformed.
  const transcript = { ...event, payload: { humanUserId: "u", transcript: true } };
  expect(parseLiveEvent(transcript)).toEqual(transcript);
  expect(parseLiveEvent({ ...event, payload: { humanUserId: "u", transcript: false } })).toBeNull();
});

test("session attention carries the owner's banner and a known state", () => {
  const event = { schemaVersion: 1, eventId: "att-1", type: "workspace.session.attention", occurredAt: "2026-09-25T00:00:00Z",
    payload: { sessionId: "s1", state: "waiting", push: { title: "Claude · Fix is waiting for you", body: "", data: { source: "session", sessionId: "s1" } } } };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, state: "join_allowed" } })).not.toBeNull();
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, state: "join_declined" } })).not.toBeNull();
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, state: "busy" } })).toBeNull();
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, push: { body: "x" } } })).toBeNull();
});
