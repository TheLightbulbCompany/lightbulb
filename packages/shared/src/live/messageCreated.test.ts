import { expect, it } from "vitest";
import { parseLiveEvent } from "./types";

const baseMessage = {
  messageId: "message",
  roomId: "room",
  messageSeq: 1,
  roomRevision: 1,
  author: { kind: "human", id: "u1", displayName: "Ann" },
  text: "hi",
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "now",
  runId: null,
};

function createdEvent(payload: Record<string, unknown> = {}) {
  return {
    schemaVersion: 1,
    eventId: "message",
    type: "room.message.created",
    occurredAt: "now",
    roomId: "room",
    messageSeq: 1,
    roomRevision: 1,
    payload: { ...baseMessage, ...payload },
  };
}

it("accepts the backend's own dispatch decision, and tolerates a backend that predates the field", () => {
  const withIds = createdEvent({ dispatchAgentIds: ["agent-1"] });
  expect(parseLiveEvent(withIds)).toEqual(withIds);
  const withoutField = createdEvent();
  expect(parseLiveEvent(withoutField)).toEqual(withoutField);
  const empty = createdEvent({ dispatchAgentIds: [] });
  expect(parseLiveEvent(empty)).toEqual(empty);
});

it("rejects a malformed dispatchAgentIds", () => {
  expect(parseLiveEvent(createdEvent({ dispatchAgentIds: "agent-1" }))).toBeNull();
  expect(parseLiveEvent(createdEvent({ dispatchAgentIds: [1] }))).toBeNull();
});
