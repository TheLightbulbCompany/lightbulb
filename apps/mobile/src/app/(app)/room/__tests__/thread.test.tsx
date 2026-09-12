import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { CanonicalRoomMessage, RoomDetail } from "@lightbulb/shared";
import { AppState } from "react-native";

// The real @lightbulb/shared room machine runs here, so a cold transform cache
// can outlast jest's 5s default.
jest.setTimeout(20000);

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest.
const mockPush = jest.fn();
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
    Stack: { Screen: () => null },
    useFocusEffect: (cb: () => void) => useEffect(cb, [cb]),
    useLocalSearchParams: () => ({ roomId: "room-1", messageId: "m1" }),
  };
});
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "44444444-4444-4444-8444-444444444444" }));

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
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null,
  liveChannels: ["/rooms/room-1/k"], connectionRevision: 1,
  messaging: {
    listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
    getRoom: jest.fn(async () => mockRoom),
    listMessages: jest.fn(async () => ({ messages: mockMessages, nextAfterSeq: null })),
    markRead: mockMarkRead,
    sendMessage: mockSendMessage,
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    downloadAttachment: jest.fn(), uploadAttachment: jest.fn(), markUnread: mockMarkUnread,
    setTyping: jest.fn(), abortRun: jest.fn(), steerRun: jest.fn(), createRoom: jest.fn(),
    updateRoom: jest.fn(), upsertMember: jest.fn(), removeMember: jest.fn(),
    updateMessage: jest.fn(), setReaction: jest.fn(), getLiveConfig: jest.fn(), listResources: jest.fn(),
  },
  claimActiveRoomSubscription: jest.fn(() => () => {}),
  onEvent: () => () => {}, onChatMessage: () => () => {},
  runtime: {}, rpc: jest.fn(), getOperation: jest.fn(),
};
// One hoisted object, never a fresh literal per call: RoomsPlatform requires a
// referentially stable `live`.
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import Thread from "../[roomId]/thread/[messageId]";

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
    mockPush.mockClear();
    mockMarkRead.mockClear();
    mockSendMessage.mockClear();
    mockMarkUnread.mockClear();
    mockMessages = [...mockBase];
  });

  it("pins the root at the top and shows only its replies below", async () => {
    await render(<Thread />);
    await waitFor(() => expect(screen.getByText("first")).toBeTruthy());
    expect(screen.getByText("second")).toBeTruthy();
    expect(screen.getByText("mine")).toBeTruthy();
    expect(screen.getByText("2 replies")).toBeTruthy();
    expect(screen.queryByText("unrelated")).toBeNull();
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
});
