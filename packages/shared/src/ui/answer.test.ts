import { describe, expect, it } from "vitest";
import type { CanonicalRoomMessage } from "../messaging/types";
import { answerIsFormOf, answerIsFrom, answerIsTableOf, cardAnswerFor, cardAnswersFor } from "./answer";

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
  it("agrees with cardAnswerFor on threads: a thread's chatter is no answer, a thread card's click is", () => {
    const human = { kind: "human" as const, id: "u", displayName: "U" };
    const top = msg({ messageId: "top" });
    const inThread = msg({ messageId: "tc", threadRootMessageId: "root-7" });
    const messages = [top, inThread,
      msg({ messageId: "chat", author: human, replyToMessageId: "top", threadRootMessageId: "top", text: "chatting" }),
      msg({ messageId: "y", author: human, replyToMessageId: "tc", threadRootMessageId: "root-7", text: "Starter" }),
      msg({ messageId: "z", author: human, replyToMessageId: "tc", threadRootMessageId: "root-7", text: "Studio" })];
    const map = cardAnswersFor(messages);
    expect([...map]).toEqual([["tc", "Starter"]]);
    for (const card of [top, inThread]) expect(map.get(card.messageId) ?? null).toBe(cardAnswerFor(messages, card));
  });
});

describe("which component an answer belongs to", () => {
  it("a chip group owns a single pick, a multi group owns a comma list of its own options", () => {
    expect(answerIsFrom("Studio", ["Starter", " Studio "])).toBe(true);
    expect(answerIsFrom("Email, Push", ["Email", "SMS", "Push"], true)).toBe(true);
    expect(answerIsFrom("Email, Fax", ["Email", "SMS", "Push"], true)).toBe(false);
    expect(answerIsFrom("Your name: Ada\nPlan: Team", ["Compare the plans"])).toBe(false);
    expect(answerIsFrom(null, ["A"])).toBe(false);
  });
  it("a form owns Label: value lines that name its labels; a table owns a markdown table with its columns", () => {
    expect(answerIsFormOf("Your name: Ada\nPlan: Team", ["Your name", "Plan"])).toBe(true);
    expect(answerIsFormOf("Studio", ["Your name", "Plan"])).toBe(false);
    expect(answerIsTableOf("| Item | Qty |\n| --- | --- |\n| Pens | 3 |", ["Item", "Qty"])).toBe(true);
    expect(answerIsTableOf("Your name: Ada", ["Item", "Qty"])).toBe(false);
  });
});
