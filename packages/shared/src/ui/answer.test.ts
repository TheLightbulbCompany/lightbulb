import { describe, expect, it } from "vitest";
import type { CanonicalRoomMessage } from "../messaging/types";
import { cardAnswerFor, cardAnswersFor } from "./answer";

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
    expect(cardAnswerFor(messages, card)).toBe("Studio");
  });
  it("ignores deleted replies and other messages", () => {
    const messages = [card, msg({ messageId: "y", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", text: "Studio", deletedAt: "2026-10-16T01:00:00Z" })];
    expect(cardAnswerFor(messages, card)).toBeNull();
    expect(cardAnswerFor(messages, { messageId: "other", threadRootMessageId: null })).toBeNull();
  });
  it("a reply typed in the card's thread is not the answer", () => {
    const messages = [card, msg({ messageId: "t", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", threadRootMessageId: "card", text: "chatting" })];
    expect(cardAnswerFor(messages, card)).toBeNull();
  });
  it("a click on a thread card is its answer", () => {
    const threadCard = msg({ messageId: "card", threadRootMessageId: "root-7" });
    const messages = [threadCard, msg({ messageId: "y", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", threadRootMessageId: "root-7", text: "Studio" })];
    expect(cardAnswerFor(messages, threadCard)).toBe("Studio");
  });
});

describe("cardAnswersFor", () => {
  it("maps every answered agent message in one pass and skips the rest", () => {
    const human = { kind: "human" as const, id: "u", displayName: "U" };
    const messages = [msg({ messageId: "c1" }), msg({ messageId: "c2" }), msg({ messageId: "h", author: human, text: "hi" }),
      msg({ messageId: "y", author: human, replyToMessageId: "c1", text: "Studio" })];
    expect([...cardAnswersFor(messages)]).toEqual([["c1", "Studio"]]);
  });
});
