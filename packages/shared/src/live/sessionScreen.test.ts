import { expect, test } from "vitest";
import { LIVE_EVENT_TYPES, parseLiveEvent } from "./types";

test("a screen frame carries one session's terminal bytes and nothing else", () => {
  const event = { schemaVersion: 1, eventId: "scr-1", type: "workspace.session.screen", occurredAt: "2026-10-03T00:00:00Z",
    payload: { sessionId: "s1", generation: "g1", epoch: 1760000000000, seq: 0, cols: 120, rows: 40, kind: "snapshot", data: "G1tI" } };
  expect(LIVE_EVENT_TYPES.has("workspace.session.screen")).toBe(true);
  expect(parseLiveEvent(event)).toEqual(event);
  for (const bad of [{ kind: "input" }, { seq: -1 }, { cols: 0 }, { rows: 501 }, { data: 7 }, { epoch: 1.5 }]) {
    expect(parseLiveEvent({ ...event, payload: { ...event.payload, ...bad } })).toBeNull();
  }
});
