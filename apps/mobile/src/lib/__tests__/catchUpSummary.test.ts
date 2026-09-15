import type { CanonicalRoomMessage } from "@lightbulb/shared";

import { buildCatchUpSummary, RECENT_SUMMARY_LIMIT } from "../catchUpSummary";

const message = (messageSeq: number, text: string): CanonicalRoomMessage => ({
  messageId: `m-${messageSeq}`,
  roomId: "room-1",
  messageSeq,
  roomRevision: messageSeq,
  author: { kind: "human", id: "user-1", displayName: "Leonardo" },
  text,
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-15T02:54:00Z",
  runId: null,
});

describe("buildCatchUpSummary", () => {
  it("uses the opening read cursor and separates stated facts from unresolved points", () => {
    const summary = buildCatchUpSummary([
      message(1, "Old context"),
      message(2, "We agreed to ship the compact header."),
      message(3, "Mina will add the tests."),
      message(4, "Should the card remember dismissal?"),
    ], 1);

    expect(summary.scope).toBe("Messages since you last viewed this chat");
    expect(summary.messageCount).toBe(3);
    expect(summary.decisions).toEqual(["Leonardo: We agreed to ship the compact header."]);
    expect(summary.actionItems).toEqual(["Leonardo: Mina will add the tests."]);
    expect(summary.openQuestions).toEqual(["Leonardo: Should the card remember dismissal?"]);
    expect(summary.mainDiscussion).not.toContain("Leonardo: Old context");
  });

  it("falls back to an honestly labeled bounded recent window without a cursor", () => {
    const messages = Array.from({ length: RECENT_SUMMARY_LIMIT + 3 }, (_, index) =>
      message(index + 1, `Update ${index + 1}`),
    );

    const summary = buildCatchUpSummary(messages, null);

    expect(summary.messageCount).toBe(RECENT_SUMMARY_LIMIT);
    expect(summary.scope).toContain("no reliable read position");
    expect(summary.mainDiscussion[0]).toBe("Leonardo: Update 4");
  });

  it("omits deleted and blank messages from every section", () => {
    const deleted = { ...message(1, "secret"), deletedAt: "2026-09-15T03:00:00Z" };
    const summary = buildCatchUpSummary([deleted, message(2, "   ")], 0);

    expect(summary.messageCount).toBe(0);
    expect(summary.mainDiscussion).toEqual([]);
  });
});
