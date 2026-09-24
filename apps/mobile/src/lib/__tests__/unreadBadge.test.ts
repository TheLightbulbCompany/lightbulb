import type { RoomSummary } from "@lightbulb/shared";

import { badgeCount } from "../unreadBadge";

const room = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "r",
  name: null,
  kind: "dm",
  roomRevision: 1,
  lastMessageSeq: 0,
  lastMessageAt: null,
  unreadCount: 0,
  updatedAt: "2026-09-11T00:00:00Z",
  topic: null,
  starred: false,
  muted: false,
  sectionId: null,
  members: [],
  lastReadSeq: 0,
  ...over,
});

describe("badgeCount", () => {
  it("counts every DM and group-DM unread", () => {
    expect(
      badgeCount([
        room({ roomId: "a", unreadCount: 2 }),
        room({ roomId: "b", kind: "group-dm", unreadCount: 3 }),
      ]),
    ).toBe(5);
  });

  it("counts a channel's mentions, never its plain unreads", () => {
    expect(badgeCount([room({ roomId: "c", kind: "channel", unreadCount: 9 })])).toBe(0);
    expect(badgeCount([room({ roomId: "c", kind: "channel", unreadCount: 9, mentionCount: 2 })])).toBe(2);
  });

  it("keeps a thread-only mention, which the top-level unreadCount excludes", () => {
    expect(badgeCount([room({ roomId: "c", kind: "channel", unreadCount: 0, mentionCount: 1 })])).toBe(1);
  });

  it("skips muted rooms of either kind", () => {
    expect(
      badgeCount([
        room({ roomId: "a", unreadCount: 4, muted: true }),
        room({ roomId: "c", kind: "channel", mentionCount: 3, muted: true }),
      ]),
    ).toBe(0);
  });
});
