import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError, type CanonicalRoomMessage, type LiveEvent, type MessagePage, type RoomDetail } from "@lightbulb/shared";
import type { ReactNode } from "react";
import { AppState } from "react-native";

// The real @lightbulb/shared room machine runs here, so a cold transform cache
// can outlast jest's 5s default.
jest.setTimeout(20000);

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest.
const mockPush = jest.fn();
// The blocked-state tests each need their own SWR cache entry (the shared
// room machine's `useSWR(["messaging-room", scope, activeRoomId], ...)` is
// keyed off this), so a getRoom override in one test can't be served from
// another test's cached "room-1" success. Reset in beforeEach.
let mockRoomId = "room-1";
let mockMessageId = "m1";
let mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
jest.mock("expo-router", () => {
  // useFocusEffect is an EFFECT upstream; calling the callback during render
  // turns the screen's setFocused into a render-phase update React re-runs
  // unconditionally ("Too many re-renders").
  const { useEffect } = require("react");
  return {
    // Lazy, always: babel lowers these `const`s to `var` and the factory runs
    // before the module body has assigned them.
    router: {
      push: (...args: unknown[]) => mockPush(...args),
      back: jest.fn(),
      canGoBack: () => true,
      replace: jest.fn(),
    },
    // F16: the screen's title now lives in `headerTitle` (a ConversationHeader),
    // not a plain `title` string — render it so a test can find the text inside.
    Stack: {
      Screen: ({ options }: { options?: { headerTitle?: () => ReactNode } }) =>
        options?.headerTitle ? options.headerTitle() : null,
    },
    useFocusEffect: (cb: () => void) => useEffect(cb, [cb]),
    useLocalSearchParams: () => ({ roomId: mockRoomId, messageId: mockMessageId }),
  };
});
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "44444444-4444-4444-8444-444444444444" }));
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => mockInsets,
}));

const mockRoom: RoomDetail = {
  roomId: "room-1", name: "Launch", kind: "channel", roomRevision: 1, deliveryKey: "k",
  pinnedMessageIds: [], topic: null, starred: false, muted: false, sectionId: null,
  lastMessageSeq: 4, lastMessageAt: "2026-09-11T10:03:00Z", unreadCount: 2,
  updatedAt: "2026-09-11T10:03:00Z", lastReadSeq: 2, createdAt: "2026-09-11T09:00:00Z",
  members: [
    { kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "x", joinedSeq: 0 },
    { kind: "agent", id: "agent-1", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "active", joinedAt: "x", joinedSeq: 0 },
  ],
};
const mockMsg = (over: Partial<CanonicalRoomMessage>): CanonicalRoomMessage => ({
  messageId: "m1", roomId: "room-1", messageSeq: 1, roomRevision: 1,
  author: { kind: "agent", id: "agent-1", displayName: "Friday" }, text: "first",
  mentions: [], replyToMessageId: null, threadRootMessageId: null,
  createdAt: "2026-09-11T10:00:00Z", runId: null, ...over,
});
// m2/m3 hang off m1; m4 is a sibling in the room that the thread must not show.
const mockBase = [
  mockMsg({}),
  mockMsg({ messageId: "m2", messageSeq: 2, text: "second", threadRootMessageId: "m1", replyToMessageId: "m1" }),
  mockMsg({ messageId: "m3", messageSeq: 3, text: "mine", threadRootMessageId: "m1", replyToMessageId: "m1", author: { kind: "human", id: "user-a", displayName: "Alice" } }),
  mockMsg({ messageId: "m4", messageSeq: 4, text: "unrelated" }),
];
let mockMessages: CanonicalRoomMessage[] = [...mockBase];

const mockMarkRead = jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 4 }));
const mockMarkUnread = jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 1 }));
const mockSendMessage = jest.fn(async () => mockMsg({ messageId: "m5", messageSeq: 5, text: "on it", threadRootMessageId: "m1" }));
const mockSetReaction = jest.fn(async () => mockMsg({ messageId: "m1", reactions: { "🎉": ["user-a"] } }));
const mockSetTyping = jest.fn(async () => {});
// Live events this suite dispatches by hand (typing changes only so far);
// everything else stays the fixed no-op the rest of the suite never drives.
const typingHandlers = new Set<(event: LiveEvent) => void>();
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null,
  // room-unavailable is the blocked-states suite's own room id (its Try again
  // recovery needs its post-retry reconcile to actually run).
  liveChannels: ["/rooms/room-1/k", "/rooms/room-unavailable/k"], connectionRevision: 1,
  messaging: {
    listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
    // Stamps whatever roomId it's asked for onto the fixture, so a
    // blocked-states test can select a fresh, never-cached room id (SWR dedupes
    // identical keys across the fast-firing tests in this file, so reusing
    // "room-1" would just replay ITS cached success and never call this mock).
    getRoom: jest.fn(async (roomId: string) => ({ ...mockRoom, roomId })),
    listMessages: jest.fn(async (roomId: string, _afterSeq?: number, _limit?: number, _options?: { beforeSeq?: number }): Promise<MessagePage> => ({
      messages: mockMessages.map((message) => ({ ...message, roomId })),
      nextAfterSeq: null,
    })),
    markRead: mockMarkRead,
    sendMessage: mockSendMessage,
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    downloadAttachment: jest.fn(), attachmentUrl: jest.fn(), uploadAttachment: jest.fn(), markUnread: mockMarkUnread,
    setTyping: mockSetTyping, abortRun: jest.fn(), steerRun: jest.fn(), listSubagents: jest.fn().mockResolvedValue({ subagents: [] }), subagentTail: jest.fn(), steerSubagent: jest.fn(), stopSubagent: jest.fn(), createRoom: jest.fn(),
    updateRoom: jest.fn(), upsertMember: jest.fn(), removeMember: jest.fn(),
    updateMessage: jest.fn(), setReaction: mockSetReaction, getLiveConfig: jest.fn(),
  },
  claimActiveRoomSubscription: jest.fn(() => () => {}),
  onEvent: (handler: (event: LiveEvent) => void) => {
    typingHandlers.add(handler);
    return () => typingHandlers.delete(handler);
  },
  onChatMessage: () => () => {},
  runtime: {}, rpc: jest.fn(), getOperation: jest.fn(),
};
// One hoisted object, never a fresh literal per call: RoomsPlatform requires a
// referentially stable `live`.
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive, useOptionalLiveEvents: () => mockLive }));

import Thread from "../[roomId]/thread/[messageId]";
import { getCurrentTarget } from "../../../../lib/activeAgent";

// jest-expo leaves AppState.currentState undefined; a device reports "active",
// and the RoomsPlatform ANDs it into isVisible(), which gates read marking.
const realAppState = AppState.currentState;
beforeAll(() => {
  (AppState as { currentState: typeof AppState.currentState }).currentState = "active";
});
afterAll(() => {
  (AppState as { currentState: typeof AppState.currentState }).currentState = realAppState;
});

describe("thread screen", () => {
  beforeEach(() => {
    mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
    mockPush.mockClear();
    mockMarkRead.mockClear();
    mockSendMessage.mockClear();
    mockMarkUnread.mockClear();
    mockSetReaction.mockClear();
    mockSetTyping.mockClear();
    typingHandlers.clear();
    mockMessages = [...mockBase];
    mockRoomId = "room-1";
    mockMessageId = "m1";
    mockLive.messaging.listRooms.mockReset().mockImplementation(async () => ({ rooms: [mockRoom] }));
    mockLive.messaging.getRoom.mockReset().mockImplementation(async (roomId: string) => ({ ...mockRoom, roomId }));
    mockLive.messaging.listMessages.mockReset().mockImplementation(async (roomId: string) => ({
      messages: mockMessages.map((message) => ({ ...message, roomId })), nextAfterSeq: null,
    }));
  });

  it("uses the stable pushed-screen offset instead of a mount-time window measurement", async () => {
    mockInsets = { top: 59, bottom: 34, left: 0, right: 0 };
    await render(<Thread />);

    const keyboardAvoider = screen.getByTestId("thread-timeline").parent;
    expect(keyboardAvoider?.props.automaticOffset).toBeUndefined();
    // Status bar 59 + nav bar 44 - home indicator 34, plus 8 of air over the keyboard.
    expect(keyboardAvoider?.props.keyboardVerticalOffset).toBe(77);
  });

  it("pins the root at the top and shows only its replies below", async () => {
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    expect(screen.getByText("second")).toBeTruthy();
    expect(screen.getByText("mine")).toBeTruthy();
    expect(screen.getByText("2 replies")).toBeTruthy();
    expect(screen.queryByText("unrelated")).toBeNull();
  });

  it("loads an older thread root after the newest room page", async () => {
    mockRoomId = "room-long";
    const server = Array.from({ length: 201 }, (_, index) => mockMsg({
      messageId: `m${index + 1}`, messageSeq: index + 1,
      text: `message ${index + 1}`,
      threadRootMessageId: index === 200 ? "m1" : null,
    }));
    mockLive.messaging.listRooms.mockResolvedValue({ rooms: [{ ...mockRoom, roomId: mockRoomId, lastMessageSeq: 201 }] });
    mockLive.messaging.getRoom.mockResolvedValue({ ...mockRoom, roomId: mockRoomId, lastMessageSeq: 201 });
    mockLive.messaging.listMessages.mockImplementation(async (_roomId, afterSeq = 0, limit = 100, options) => {
      if (options?.beforeSeq !== undefined) {
        const messages = server.filter((item) => item.messageSeq < options.beforeSeq!).slice(-limit);
        return { messages: messages.map((item) => ({ ...item, roomId: mockRoomId })), nextAfterSeq: null, nextBeforeSeq: messages[0]?.messageSeq > 1 ? messages[0].messageSeq : null };
      }
      const messages = server.filter((item) => item.messageSeq > afterSeq).slice(0, limit);
      return { messages: messages.map((item) => ({ ...item, roomId: mockRoomId })), nextAfterSeq: messages.length === limit ? messages.at(-1)!.messageSeq : null };
    });
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy());
    expect(screen.getByText("message 201")).toBeTruthy();
    expect(mockLive.messaging.listMessages).toHaveBeenCalledWith("room-long", 0, 100, expect.objectContaining({ beforeSeq: 202 }));
    expect(mockLive.messaging.listMessages).toHaveBeenCalledWith("room-long", 0, 100, expect.any(Object));
  });

  it("keeps a recent thread on the newest page", async () => {
    mockRoomId = "room-recent";
    const server = Array.from({ length: 201 }, (_, index) => mockMsg({
      messageId: `m${index + 1}`, messageSeq: index + 1,
      text: `message ${index + 1}`,
      threadRootMessageId: index === 200 ? "m1" : null,
    }));
    // This route targets a root inside the newest page.
    server[0] = { ...server[0], messageId: "old-root" };
    server[149] = { ...server[149], messageId: "m1", threadRootMessageId: null };
    mockLive.messaging.listRooms.mockResolvedValue({ rooms: [{ ...mockRoom, roomId: mockRoomId, lastMessageSeq: 201 }] });
    mockLive.messaging.getRoom.mockResolvedValue({ ...mockRoom, roomId: mockRoomId, lastMessageSeq: 201 });
    mockLive.messaging.listMessages.mockImplementation(async (_roomId, afterSeq = 0, limit = 100, options) => {
      if (options?.beforeSeq !== undefined) {
        const messages = server.filter((item) => item.messageSeq < options.beforeSeq!).slice(-limit);
        return { messages: messages.map((item) => ({ ...item, roomId: mockRoomId })), nextAfterSeq: null, nextBeforeSeq: 102 };
      }
      return { messages: server.filter((item) => item.messageSeq > afterSeq).slice(0, limit).map((item) => ({ ...item, roomId: mockRoomId })), nextAfterSeq: null };
    });
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("message 150")).toBeTruthy());
    expect(screen.getByText("message 201")).toBeTruthy();
    expect(mockLive.messaging.listMessages).toHaveBeenCalledWith("room-recent", 0, 100, expect.objectContaining({ beforeSeq: 202 }));
    expect(mockLive.messaging.listMessages.mock.calls.some(([, afterSeq, , options]) => afterSeq === 0 && options?.beforeSeq === undefined)).toBe(false);
  });

  it("waits for a changed room before requesting an older thread root", async () => {
    const nextRoom = Array.from({ length: 201 }, (_, index) => mockMsg({
      roomId: "room-next", messageId: index === 0 ? "old-root" : `m${index + 1}`,
      messageSeq: index + 1, text: `message ${index + 1}`,
      threadRootMessageId: index === 200 ? "old-root" : null,
    }));
    mockLive.messaging.getRoom.mockImplementation(async (roomId) => ({ ...mockRoom, roomId, lastMessageSeq: roomId === "room-next" ? 201 : 2 }));
    mockLive.messaging.listMessages.mockImplementation(async (roomId, afterSeq = 0, limit = 100, options) => {
      if (roomId !== "room-next") {
        return options?.beforeSeq !== undefined
          ? { messages: [mockBase[0]], nextAfterSeq: null, nextBeforeSeq: 1 }
          : { messages: [], nextAfterSeq: null };
      }
      if (options?.beforeSeq !== undefined) {
        const messages = nextRoom.filter((item) => item.messageSeq < options.beforeSeq!).slice(-limit);
        return { messages, nextAfterSeq: null, nextBeforeSeq: messages[0]?.messageSeq > 1 ? messages[0].messageSeq : null };
      }
      const messages = nextRoom.filter((item) => item.messageSeq > afterSeq).slice(0, limit);
      return { messages, nextAfterSeq: messages.length === limit ? messages.at(-1)!.messageSeq : null };
    });
    const view = await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    mockRoomId = "room-next";
    mockMessageId = "old-root";
    view.rerender(<Thread />);
    // Paging 201 messages back to the root takes several reads: past the default 1 s on a busy
    // CI runner (it failed there on unrelated branches too).
    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy(), { timeout: 5000 });
    expect(mockLive.messaging.listMessages).toHaveBeenCalledWith("room-next", 0, 100, expect.objectContaining({ beforeSeq: 202 }));
    expect(mockLive.messaging.listMessages.mock.calls.some(([roomId, afterSeq, , options]) => roomId === "room-next" && afterSeq === 0 && options?.beforeSeq === undefined)).toBe(true);
  });

  it("sends a reply carrying both thread ids", async () => {
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Reply…"), "on it");
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() =>
      expect(mockSendMessage).toHaveBeenCalledWith(
        "room-1",
        expect.objectContaining({ text: "on it", replyToMessageId: "m1", threadRootMessageId: "m1" }),
      ),
    );
  });

  it("titles itself Thread and names the conversation underneath", async () => {
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("#Launch")).toBeTruthy());
  });

  // Slack's rule: a thread is not the room. The read cursor is per room and
  // Phase A adds no per-thread state, so a thread marks nothing read.
  it("never marks the room read, even though the room has unread messages", async () => {
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    expect(mockMarkRead).not.toHaveBeenCalled();
  });

  // The web's `!inThread` rule, both halves: a thread reply cannot open a nested
  // thread, and the read cursor is per ROOM — marking the room unread from here
  // is undone the moment the conversation behind this screen is re-focused.
  // The conversation screen keeps the action (see conversation.test.tsx).
  it("offers neither a nested thread nor Mark unread on a reply", async () => {
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    await fireEvent(screen.getByText("second"), "longPress");
    // The sheet IS open — these two are absent, not the whole sheet.
    expect(screen.getByText("Copy text")).toBeTruthy();
    expect(screen.queryByText("Reply in thread")).toBeNull();
    expect(screen.queryByText("Mark unread")).toBeNull();
    expect(mockMarkUnread).not.toHaveBeenCalled();
  });

  it("labels a reply that failed to send and retries it", async () => {
    mockSendMessage.mockRejectedValueOnce(new Error("offline"));
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Reply…"), "will fail");
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() => expect(screen.getByText("Not sent · tap to retry")).toBeTruthy());
    expect(screen.getByText("will fail")).toBeTruthy();
    await fireEvent.press(screen.getByText("Not sent · tap to retry"));
    await waitFor(() => expect(screen.queryByText("Not sent · tap to retry")).toBeNull());
    expect(mockSendMessage).toHaveBeenCalledTimes(2);
  });

  // The bug this task exists to fix: this screen used to swallow
  // `setReaction`'s rejection with `.catch(() => {})`. Exercises the real
  // `reactToMessage` path end to end, including the unconditional-clear
  // timing: a rejection on the reply must not survive a SUCCESSFUL toggle on
  // the pinned root.
  it("surfaces a failed reaction toggle and clears it on a later success elsewhere", async () => {
    mockSetReaction.mockRejectedValueOnce(new Error("offline"));
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    await fireEvent(screen.getByText("second"), "longPress");
    await fireEvent.press(screen.getByLabelText("React with 🎉"));
    await waitFor(() =>
      expect(screen.getByText("Could not update reaction. Try again.")).toBeTruthy(),
    );
    // A successful toggle on the pinned root ("first") still clears it.
    await fireEvent(screen.getByText("first"), "longPress");
    await fireEvent.press(screen.getByLabelText("React with 🎉"));
    await waitFor(() =>
      expect(screen.queryByText("Could not update reaction. Try again.")).toBeNull(),
    );
  });

  // The threadId half is what keeps a reply to THIS thread suppressed while a
  // reply to a sibling thread of the same room still banners.
  it("claims the push target for this thread, not just its room", async () => {
    const { unmount } = await render(<Thread />);
    expect(getCurrentTarget()).toMatchObject({ roomId: "room-1", threadId: "m1" });
    await unmount();
    expect(getCurrentTarget()).toMatchObject({ roomId: null, threadId: null });
  });

  // Mirrors web's RoomPanel `room-blocked` guard (search `room-blocked`):
  // the whole panel is replaced, not banner-over-composer.
  describe("blocked states", () => {
    it("shows the loading state with neither timeline nor composer while the room is loading", async () => {
      mockRoomId = "room-loading";
      mockLive.messaging.getRoom.mockImplementationOnce(() => new Promise(() => {}));
      await render(<Thread />);
      expect(screen.getByText("Loading chat…")).toBeTruthy();
      expect(screen.queryByTestId("thread-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText("Reply…")).toBeNull();
    });

    it("shows the access-removed state with no composer and no retry once access was rotated away", async () => {
      mockRoomId = "room-access-removed";
      mockLive.messaging.getRoom.mockRejectedValueOnce(new ApiError(403, null));
      await render(<Thread />);
      await waitFor(() =>
        expect(
          screen.getByText("Access removed. You can no longer view this chat."),
        ).toBeTruthy(),
      );
      expect(screen.queryByTestId("thread-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText("Reply…")).toBeNull();
      expect(screen.queryByText("Try again")).toBeNull();
    });

    it("shows the unavailable state, and Try again recovers the thread", async () => {
      mockRoomId = "room-unavailable";
      mockLive.messaging.getRoom.mockRejectedValueOnce(new Error("boom"));
      await render(<Thread />);
      await waitFor(() =>
        expect(screen.getByText("This chat is unavailable.")).toBeTruthy(),
      );
      expect(screen.queryByTestId("thread-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText("Reply…")).toBeNull();
      await fireEvent.press(screen.getByText("Try again"));
      await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
      expect(screen.getByPlaceholderText("Reply…")).toBeTruthy();
    });
  });

  it("reports typing to the room as the reply draft changes", async () => {
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Reply…"), "on it");
    await waitFor(() =>
      expect(mockSetTyping).toHaveBeenCalledWith({ roomId: "room-1", isTyping: true }),
    );
  });

  it("keeps a thread run's background subagents under its saved reply", async () => {
    // Its own room: the shared history cache outlives a case, and this reply must not reach the next one.
    mockRoomId = "room-delegate";
    mockLive.messaging.listRooms.mockResolvedValue({ rooms: [{ ...mockRoom, roomId: mockRoomId }] });
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    const emit = (event: LiveEvent) => act(async () => { for (const handler of typingHandlers) handler(event); });
    const frame = (body: Record<string, unknown>) => ({ type: "room.run.frame",
      payload: { roomId: "room-delegate", agentId: "agent-1", runId: "run-t", threadRootMessageId: "m1", frame: body } } as LiveEvent);
    await emit(frame({ type: "tool_start", runId: "run-t", tool: "delegate_task", toolCallId: "t1", args: {} }));
    await emit(frame({ type: "tool_end", runId: "run-t", tool: "delegate_task", toolCallId: "t1", result: JSON.stringify({ delegation_id: "d-1" }) }));
    const reply = mockMsg({ messageId: "m6", messageSeq: 6, text: "helpers started", threadRootMessageId: "m1", replyToMessageId: "m1", runId: "run-t", roomId: "room-delegate" });
    await emit({ schemaVersion: 1, eventId: "e-6", type: "room.message.created", occurredAt: reply.createdAt, roomId: "room-delegate", messageSeq: 6, roomRevision: 1, payload: reply } as LiveEvent);
    await emit(frame({ type: "run_ended", outcome: "done" }));
    await waitFor(() => expect(screen.getByText("helpers started")).toBeTruthy());
    expect(screen.getByText("Background subagents dispatched")).toBeTruthy();
  });

  it("shows another member's typing line with the web's exact wording", async () => {
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    const expiresAt = new Date(Date.now() + 5_000).toISOString();
    await act(async () => {
      for (const handler of typingHandlers) {
        handler({
          type: "room.typing.changed",
          payload: { roomId: "room-1", userId: "user-b", displayName: "Bob", expiresAt },
        } as LiveEvent);
      }
    });
    await waitFor(() => expect(screen.getByLabelText("Bob is typing…")).toBeTruthy());
  });

  it("draws the viewer's reply as a mine bubble with Sent under it", async () => {
    await render(<Thread />);
    // Fixture: the thread's newest reply is the viewer's (user-a), nothing after it.
    await waitFor(() => expect(screen.getByText("Sent")).toBeTruthy());
    const bubbles = screen.getAllByTestId("message-bubble");
    expect(bubbles.length).toBeGreaterThan(1);
  });
});
