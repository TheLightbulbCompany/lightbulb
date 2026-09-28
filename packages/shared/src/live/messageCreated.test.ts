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

it("accepts the agents this message addresses, and tolerates a backend that predates the field", () => {
  const withIds = createdEvent({ addressedAgentIds: ["agent-1"] });
  expect(parseLiveEvent(withIds)).toEqual(withIds);
  const withoutField = createdEvent();
  expect(parseLiveEvent(withoutField)).toEqual(withoutField);
  const empty = createdEvent({ addressedAgentIds: [] });
  expect(parseLiveEvent(empty)).toEqual(empty);
});

it("rejects a malformed addressedAgentIds", () => {
  expect(parseLiveEvent(createdEvent({ addressedAgentIds: "agent-1" }))).toBeNull();
  expect(parseLiveEvent(createdEvent({ addressedAgentIds: [1] }))).toBeNull();
});
