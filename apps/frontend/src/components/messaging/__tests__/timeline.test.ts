import { describe, expect, it } from "vitest";
import { buildTimeline, dateDividerLabel } from "../timeline";
import type { RoomMessage } from "../types";

const base: RoomMessage = { messageId: "m", roomId: "r", messageSeq: 0, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Friday" }, text: "x", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-11T10:00:00Z", runId: null };
const at = (seq: number, iso: string, author = base.author, extra: Partial<RoomMessage> = {}): RoomMessage => ({ ...base, messageId: `m${seq}`, messageSeq: seq, createdAt: iso, author, ...extra });
const owner = { kind: "human" as const, id: "owner", displayName: "Owner" };
// Built from local-time components (not a UTC ISO string) so this instant is
// pinned to the same local calendar day on every host timezone.
const now = new Date(2026, 8, 11, 12, 0);

describe("buildTimeline", () => {
  it("collapses consecutive same-author messages within five minutes", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:04:59Z"), at(3, "2026-09-11T10:05:00Z")], { readSeq: null, now });
    const messages = items.filter((item) => item.kind === "message");
    expect(messages.map((item) => item.kind === "message" && item.collapsed)).toEqual([false, true, true]);
  });
  it("does not collapse across authors, deletions, or a five-minute gap", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z", owner), at(3, "2026-09-11T10:02:00Z", owner, { deletedAt: "2026-09-11T10:03:00Z" }), at(4, "2026-09-11T10:03:00Z", owner), at(5, "2026-09-11T10:09:00Z", owner)], { readSeq: null, now });
    expect(items.filter((item) => item.kind === "message").map((item) => item.kind === "message" && item.collapsed)).toEqual([false, false, false, false, false]);
  });
  it("emits one date divider per local day and never collapses across it", () => {
    // Local-time constructors, not UTC ISO literals: these two instants must
    // straddle a local calendar-day boundary on every host timezone.
    const lateYesterday = new Date(2026, 8, 10, 23, 59).toISOString();
    const earlyToday = new Date(2026, 8, 11, 0, 1).toISOString();
    const items = buildTimeline([at(1, lateYesterday), at(2, earlyToday)], { readSeq: null, now });
    expect(items.map((item) => item.kind)).toEqual(["date", "message", "date", "message"]);
    expect(items[3]).toMatchObject({ kind: "message", collapsed: false });
  });
  it("emits the New line before the first unread message and breaks grouping there", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z"), at(3, "2026-09-11T10:02:00Z")], { readSeq: 1, now });
    expect(items.map((item) => item.kind)).toEqual(["date", "message", "new", "message", "message"]);
    expect(items[3]).toMatchObject({ kind: "message", collapsed: false });
    expect(items[4]).toMatchObject({ kind: "message", collapsed: true });
  });
  it("emits no New line when everything is read or readSeq is null", () => {
    const messages = [at(1, "2026-09-11T10:00:00Z")];
    expect(buildTimeline(messages, { readSeq: 1, now }).some((item) => item.kind === "new")).toBe(false);
    expect(buildTimeline(messages, { readSeq: null, now }).some((item) => item.kind === "new")).toBe(false);
  });
  it("gives every item a stable unique key", () => {
    const items = buildTimeline([at(1, "2026-09-11T10:00:00Z"), at(2, "2026-09-11T10:01:00Z")], { readSeq: 1, now });
    expect(new Set(items.map((item) => item.key)).size).toBe(items.length);
  });
});

describe("dateDividerLabel", () => {
  it("labels today, yesterday, weekday within a week, otherwise a full date", () => {
    // Local-time constructors so "today"/"yesterday" are pinned to `now`'s
    // local calendar day on every host timezone, not just hosts near UTC.
    const today = new Date(2026, 8, 11, 9);
    const yesterday = new Date(2026, 8, 10, 9);
    const withinWeek = new Date(2026, 8, 8, 9);
    const overWeek = new Date(2026, 7, 1, 9);
    expect(dateDividerLabel(today, now)).toBe("Today");
    expect(dateDividerLabel(yesterday, now)).toBe("Yesterday");
    expect(dateDividerLabel(withinWeek, now)).toBe(withinWeek.toLocaleDateString(undefined, { weekday: "long" }));
    expect(dateDividerLabel(overWeek, now)).toBe(overWeek.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" }));
  });
});
