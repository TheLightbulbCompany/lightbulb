import { describe, expect, it } from "vitest";
import type { CanonicalRoomMessage } from "../messaging/types";
import { cardAnswerFor } from "./answer";

const msg = (over: Partial<CanonicalRoomMessage>): CanonicalRoomMessage => ({
  messageId: "m", roomId: "r", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "a", displayName: "A" },
  text: "", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-10-16T00:00:00Z", runId: null, ...over,
});

describe("cardAnswerFor", () => {
  const card = msg({ messageId: "card" });
  it("is the first human reply to the card", () => {
    const messages = [card,
      msg({ messageId: "x", author: { kind: "agent", id: "b", displayName: "B" }, replyToMessageId: "card", text: "agent noise" }),
      msg({ messageId: "y", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", text: "Studio" }),
      msg({ messageId: "z", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", text: "Starter" })];
    expect(cardAnswerFor(messages, "card")).toBe("Studio");
  });
  it("ignores deleted replies and other messages", () => {
    const messages = [card, msg({ messageId: "y", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", text: "Studio", deletedAt: "2026-10-16T01:00:00Z" })];
    expect(cardAnswerFor(messages, "card")).toBeNull();
    expect(cardAnswerFor(messages, "other")).toBeNull();
  });
});
