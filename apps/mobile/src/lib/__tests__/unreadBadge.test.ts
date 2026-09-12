import type { LiveEvent, RoomSummary } from "@lightbulb/shared";
import { act, renderHook } from "@testing-library/react-native";

import { badgeCount, useChannelMentions } from "../unreadBadge";

// `mock`-prefixed and read lazily inside the factory: babel-plugin-jest-hoist
// rejects non-`mock` out-of-scope references, and lowers the const to a var
// that is still undefined while the module under test is resolving.
const mockHandlers = new Set<(event: LiveEvent) => void>();
jest.mock("../../providers/live-events", () => ({
  useLiveEvents: () => ({
    onEvent: (handler: (event: LiveEvent) => void) => {
      mockHandlers.add(handler);
      return () => mockHandlers.delete(handler);
    },
  }),
}));

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
      badgeCount(
        [
          room({ roomId: "a", unreadCount: 2 }),
          room({ roomId: "b", kind: "group-dm", unreadCount: 3 }),
        ],
        new Map(),
      ),
    ).toBe(5);
  });
  it("ignores plain channel unreads", () => {
    expect(badgeCount([room({ roomId: "c", kind: "channel", unreadCount: 9 })], new Map())).toBe(0);
  });
  it("counts channel mentions, never more than that channel's unread total", () => {
    const rooms = [room({ roomId: "c", kind: "channel", unreadCount: 2 })];
    expect(badgeCount(rooms, new Map([["c", 1]]))).toBe(1);
    expect(badgeCount(rooms, new Map([["c", 7]]))).toBe(2);
  });
  it("drops a channel's mentions once the server reports it read", () => {
    expect(
      badgeCount([room({ roomId: "c", kind: "channel", unreadCount: 0 })], new Map([["c", 4]])),
    ).toBe(0);
  });
  // Slack's rule: a muted conversation never badges, DM or channel mention.
  it("counts nothing from a muted room", () => {
    expect(badgeCount([room({ roomId: "a", unreadCount: 4, muted: true })], new Map())).toBe(0);
    expect(
      badgeCount(
        [room({ roomId: "c", kind: "channel", unreadCount: 3, muted: true })],
        new Map([["c", 3]]),
      ),
    ).toBe(0);
  });
});

describe("useChannelMentions", () => {
  const available = (roomId: string, notify: boolean): LiveEvent =>
    ({
      type: "room.message.available",
      payload: { roomId, messageId: "m1", messageSeq: 1, threadRootMessageId: null, notify },
    }) as LiveEvent;

  beforeEach(() => mockHandlers.clear());

  it("forgets a channel's mentions once the server reports it read", async () => {
    const channel = (unreadCount: number) => [
      room({ roomId: "c1", kind: "channel", unreadCount }),
    ];
    const { result, rerender } = await renderHook(
      ({ rooms }: { rooms: RoomSummary[] }) => useChannelMentions(rooms),
      { initialProps: { rooms: channel(2) } },
    );
    await act(() => {
      for (const handler of mockHandlers) handler(available("c1", true));
    });
    expect(badgeCount(channel(2), result.current)).toBe(1);

    // The reader opens the channel; the server's unreadCount drops to 0.
    await rerender({ rooms: channel(0) });
    expect(result.current.get("c1")).toBeUndefined();

    // Three plain messages arrive. The read mention must not come back.
    expect(badgeCount(channel(3), result.current)).toBe(0);
  });

  it("counts only notifying messages, per room", async () => {
    const { result } = await renderHook(() => useChannelMentions([]));
    const emit = (event: LiveEvent) =>
      act(() => {
        for (const handler of mockHandlers) handler(event);
      });
    await emit(available("c1", true));
    await emit(available("c1", true));
    await emit(available("c1", false));
    await emit(available("c2", true));
    await emit({ type: "room.typing.changed", payload: { roomId: "c1" } } as LiveEvent);
    expect(result.current.get("c1")).toBe(2);
    expect(result.current.get("c2")).toBe(1);
  });
});
