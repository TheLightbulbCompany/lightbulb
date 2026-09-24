import { expect, it } from "vitest";
import { parseLiveEvent } from "./types";

it("validates edit invalidations without accepting cross-room pointers", () => {
  const event = { schemaVersion: 1, eventId: "edit", type: "room.message.updated", occurredAt: "now", roomId: "room",
    payload: { roomId: "room", messageId: "message", messageSeq: 1 } };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, roomId: "other" } })).toBeNull();
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, messageSeq: -1 } })).toBeNull();
});

it("accepts content-free message hints and rejects malformed sequence or notification flags", () => {
  const event = { schemaVersion: 1, eventId: "hint", type: "room.message.available", occurredAt: "now", roomId: "room", messageSeq: 2,
    payload: { roomId: "room", messageId: "message", messageSeq: 2, threadRootMessageId: null, notify: true } };
  expect(parseLiveEvent(event)).toEqual(event);
  expect(parseLiveEvent({ ...event, messageSeq: 3 })).toBeNull();
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, notify: "true" } })).toBeNull();
  expect(parseLiveEvent({ ...event, payload: { ...event.payload, roomId: "other" } })).toBeNull();
});

it("passes the preview fields through and tolerates optional fields added later", () => {
  const lastMessage = { messageId: "message", seq: 2, authorId: "u", authorKind: "human", authorName: "Ann",
    textPreview: "hi", hasAttachments: false, createdAt: "now", threadRootMessageId: null };
  const event = { schemaVersion: 1, eventId: "hint", type: "room.message.available", occurredAt: "now", roomId: "room", messageSeq: 2,
    payload: { roomId: "room", messageId: "message", messageSeq: 2, threadRootMessageId: null, notify: false,
      lastMessage, mentionedUserIds: ["u2"], previewOnly: true, push: true } };
  expect(parseLiveEvent(event)).toEqual(event);
});
