import { expect, test } from "vitest";
import { parseLiveEvent } from "./types";

test("digest.ready carries the counts and the server-rendered push", () => {
  const push = { title: "3 things need you", body: "2 approvals · 1 mention", data: { url: "https://x/chat?panel=activity" } };
  const event = {
    schemaVersion: 1, eventId: "d-1", type: "digest.ready", occurredAt: "2026-09-24T12:00:00Z",
    payload: { countsByKind: { approval: 2, assigned_issue: 0, mention: 1 }, total: 3, activityUrl: push.data.url, push },
  };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, push: { data: push.data } } })).toBeNull();
});
