import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import {
  ApiError,
  dateDividerLabel,
  type CanonicalRoomMessage,
  type LiveEvent,
  type RoomDetail,
  type RoomResources,
} from "@lightbulb/shared";
import type { ReactNode } from "react";
import { AppState } from "react-native";

// The real @lightbulb/shared room machine runs here (that is the point of the
// suite), so a cold transform cache can outlast jest's 5s default.
jest.setTimeout(20000);

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest.
const mockPush = jest.fn();
// A handle on the focus effect, so a test can drive the blur/re-focus a pushed
// thread screen causes. Upstream this is navigation; here it is the only way to
// reach the transition at all.
const mockFocus: { blur?: () => void; focus?: () => void } = {};
// The blocked-state tests each need their own SWR cache entry (the shared
// room machine's `useSWR(["messaging-room", scope, activeRoomId], ...)` is
// keyed off this), so a getRoom override in one test can't be served from
// another test's cached "room-1" success. Reset in beforeEach.
let mockRoomId = "room-1";
jest.mock("expo-router", () => {
  // useFocusEffect is an EFFECT upstream. A mock that calls the callback during
  // render turns the screen's setFocused into a render-phase update, which React
  // re-runs unconditionally — "Too many re-renders" regardless of the value.
  const { useEffect } = require("react");
  return {
    // Every reference out of a mock factory has to be LAZY: babel lowers these
    // `const`s to `var`, and the factory runs while the screen's own import is
    // still being resolved, i.e. before the module body has assigned them.
    router: {
      push: (...args: unknown[]) => mockPush(...args),
      back: jest.fn(),
      canGoBack: () => true,
      replace: jest.fn(),
    },
    // Real react-navigation renders `options.headerRight` inside the native
    // header; this mock is the whole navigator here, so it has to do that
    // itself or the header's star/mute buttons would never mount at all.
    Stack: {
      Screen: ({ options }: { options?: { headerRight?: () => ReactNode } }) =>
        options?.headerRight ? options.headerRight() : null,
    },
    useFocusEffect: (cb: () => undefined | (() => void)) =>
      useEffect(() => {
        let cleanup = cb();
        mockFocus.blur = () => {
          cleanup?.();
          cleanup = undefined;
        };
        mockFocus.focus = () => {
          cleanup = cb();
        };
        return () => cleanup?.();
      }, [cb]),
    useLocalSearchParams: () => ({ roomId: mockRoomId }),
  };
});
// Each case runs in its own workspace scope, because SWR keys its cache on it
// and the app mounts no SWRConfig provider — so without this every case in this
// file shares one module-level cache and the suite is order-dependent. It
// surfaces now because marking a room read updates that cache in place (the
// shared hook applies the server's returned cursor instead of re-fetching the
// room AND the room list after every PUT /read): a room left read by one case
// starts the next already caught up, with no unread boundary to draw and
// nothing left to mark. Every case passes alone while three fail together.
//
// The scope and not the user, because the screen reads `userId` and asserts on
// it; `useOrganization` it never touches.
let mockScope = 0;
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: { id: `org-${mockScope}` } }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

const mockRoom: RoomDetail = {
  roomId: "room-1", name: "Launch", kind: "channel", roomRevision: 1, deliveryKey: "k",
  pinnedMessageIds: [], topic: null, starred: false, muted: false, sectionId: null,
  lastMessageSeq: 3, lastMessageAt: "2026-06-11T10:02:00Z", unreadCount: 1,
  updatedAt: "2026-06-11T10:02:00Z", lastReadSeq: 2, createdAt: "2026-06-11T09:00:00Z",
  members: [
    { kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "x", joinedSeq: 0 },
    { kind: "agent", id: "agent-1", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "active", joinedAt: "x", joinedSeq: 0 },
  ],
};
const mockSession = {
  sessionId: "sess-1", deviceId: "mac-1", humanUserId: "user-a", provider: "claude" as const, generation: "g1",
  status: "busy" as const, providerVersion: "2.1.224", controlUnavailableReason: null, activeTurnId: "t1",
  capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 4, title: "Fix relay", cwdName: "lightbulb",
};
// Fixed more than a week in the past (not "recent" by any calendar reckoning at
// any run date) so `dateDividerLabel` always renders the absolute-date branch —
// never "Today"/"Yesterday" or a bare weekday name, which could otherwise
// collide with the fixture agent's display name "Friday" on the calendar week
// it happens to land on.
const mockMsg = (over: Partial<CanonicalRoomMessage>): CanonicalRoomMessage => ({
  messageId: "m1", roomId: "room-1", messageSeq: 1, roomRevision: 1,
  author: { kind: "agent", id: "agent-1", displayName: "Friday" }, text: "first",
  mentions: [], replyToMessageId: null, threadRootMessageId: null,
  createdAt: "2026-06-11T10:00:00Z", runId: null, ...over,
});
const mockBase = [
  mockMsg({}),
  mockMsg({ messageId: "m2", messageSeq: 2, text: "second" }),
  mockMsg({ messageId: "m3", messageSeq: 3, text: "mine", author: { kind: "human", id: "user-a", displayName: "Alice" } }),
];
let mockMessages: CanonicalRoomMessage[] = [...mockBase];

const mockMarkRead = jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 3 }));
const mockMarkUnread = jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 1 }));
const mockSetMemberPrefs = jest.fn(async (roomId: string, prefs: Partial<RoomDetail>) => ({
  ...mockRoom,
  roomId,
  ...prefs,
}));
const mockSetReaction = jest.fn(async () => mockMsg({ messageId: "m2", messageSeq: 2, text: "second", reactions: { "🎉": ["user-a"] } }));
const mockSendMessage = jest.fn(async () => mockMsg({ messageId: "m4", messageSeq: 4, text: "sent" }));
const mockSetTyping = jest.fn(async () => {});
// Live events this suite dispatches by hand (typing changes only so far);
// everything else stays the fixed no-op the rest of the suite never drives.
const typingHandlers = new Set<(event: LiveEvent) => void>();
// Named and hoisted so a single test can override its resolved value; every
// other case gets the empty snapshot reset in beforeEach, since the header's
// devices poll now runs on every mount and must never hand `undefined` to
// the resources hook.
const mockListResources = jest.fn(async (): Promise<RoomResources> => ({ devices: [], sessions: [], pairedNodes: [] }));
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
    listMessages: jest.fn(async (roomId: string) => ({
      messages: mockMessages.map((message) => ({ ...message, roomId })),
      nextAfterSeq: null,
    })),
    markRead: mockMarkRead,
    sendMessage: mockSendMessage,
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    downloadAttachment: jest.fn(), uploadAttachment: jest.fn(), markUnread: mockMarkUnread,
    setTyping: mockSetTyping, abortRun: jest.fn(), steerRun: jest.fn(), createRoom: jest.fn(),
    updateRoom: jest.fn(), upsertMember: jest.fn(), removeMember: jest.fn(),
    updateMessage: jest.fn(), setReaction: mockSetReaction, getLiveConfig: jest.fn(), listResources: mockListResources,
    setMemberPrefs: mockSetMemberPrefs, putSections: jest.fn(),
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
// referentially stable `live`, and a new one per render re-fires the history
// reconcile into "Maximum update depth exceeded".
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import Conversation from "../[roomId]/index";
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

describe("conversation screen", () => {
  beforeEach(() => {
    mockScope += 1;
    mockPush.mockClear();
    mockMarkRead.mockClear();
    mockSendMessage.mockClear();
    mockMarkUnread.mockClear();
    mockSetReaction.mockClear();
    mockSetTyping.mockClear();
    typingHandlers.clear();
    mockSetMemberPrefs.mockClear();
    mockListResources.mockReset();
    mockListResources.mockResolvedValue({ devices: [], sessions: [], pairedNodes: [] });
    mockMessages = [...mockBase];
    mockRoomId = "room-1";
  });

  it("shows the author name once per run and a day divider above the first message", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    // "Friday" is this fixture's agent display name, and dateDividerLabel
    // renders a weekday name for a message less than a week old — so the
    // fixture dates are pinned to 2026-06-11, far outside that window, and the
    // divider is always a calendar date. MessageGroup's author label is the one
    // rendered single-line (numberOfLines={1}); filtering on that keeps this
    // assertion honest even if the two texts ever coincide again.
    const fridayNodes = screen.getAllByText("Friday");
    const authorNames = fridayNodes.filter((node) => node.props.numberOfLines === 1);
    expect(authorNames).toHaveLength(1);
    expect(screen.getByTestId("room-timeline")).toBeTruthy();
    expect(
      screen.getByText(dateDividerLabel(new Date("2026-06-11T10:00:00Z"), new Date())),
    ).toBeTruthy();
  });

  it("draws the New messages line at the read cursor the room opened with", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("New messages")).toBeTruthy());
  });

  it("marks the room read up to the newest sequence once it is on screen", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(mockMarkRead).toHaveBeenCalledWith("room-1", { lastReadSeq: 3 }));
  });

  // The thread screen deliberately marks nothing read (Slack's rule), so the
  // room's cursor only moves again once this screen is focused back.
  //
  // Returning to a room whose cursor already sits at the newest sequence has
  // nothing to move, so it writes nothing. This used to re-send the identical
  // cursor on every focus: marking read re-fetched the room, the re-fetch
  // restored the server's older `lastReadSeq`, and the guard meant to stop a
  // redundant write never saw the value just written. Read state is applied
  // from the server's returned cursor now, so the guard holds and a pointless
  // PUT /read per focus is gone.
  it("does not re-mark a room whose cursor already sits at the newest sequence when a thread screen is popped", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(mockMarkRead).toHaveBeenCalledWith("room-1", { lastReadSeq: 3 }));
    await act(async () => mockFocus.blur?.());
    mockMarkRead.mockClear();
    await act(async () => mockFocus.focus?.());
    await act(async () => { await Promise.resolve(); });
    expect(mockMarkRead).not.toHaveBeenCalled();
  });

  it("keeps thread replies out of the main timeline and opens the thread from the replies row", async () => {
    mockMessages = [
      ...mockBase,
      mockMsg({ messageId: "m5", messageSeq: 4, text: "in the thread", threadRootMessageId: "m2" }),
    ];
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("1 reply")).toBeTruthy());
    expect(screen.queryByText("in the thread")).toBeNull();
    await fireEvent.press(screen.getByText("1 reply"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1/thread/m2");
  });

  it("shows no replies row on a message nobody replied to", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    expect(screen.queryByText("1 reply")).toBeNull();
  });

  it("labels a send that failed, keeps its text, and retries it", async () => {
    mockSendMessage.mockRejectedValueOnce(new Error("offline"));
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Message"), "will fail");
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() => expect(screen.getByText("Not sent · tap to retry")).toBeTruthy());
    // The text the user typed must survive the failure — the input is already cleared.
    expect(screen.getByText("will fail")).toBeTruthy();
    await fireEvent.press(screen.getByText("Not sent · tap to retry"));
    await waitFor(() => expect(screen.queryByText("Not sent · tap to retry")).toBeNull());
    expect(mockSendMessage).toHaveBeenCalledTimes(2);
  });

  it("says only that a channel has no messages yet, never the agent line", async () => {
    mockMessages = [];
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("No messages yet")).toBeTruthy());
    expect(screen.queryByText("Start a conversation with your agent")).toBeNull();
  });

  it("keeps the keyboard dismissable by drag and taps landing on controls", async () => {
    await render(<Conversation />);
    const list = screen.getByTestId("room-timeline");
    expect(list.props.keyboardDismissMode).toBe("interactive");
    expect(list.props.keyboardShouldPersistTaps).toBe("handled");
  });

  it("opens the actions sheet on a long press and acts on the message pressed", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    expect(screen.queryByText("Mark unread")).toBeNull();
    await fireEvent(screen.getByText("second"), "longPress");
    await fireEvent.press(screen.getByLabelText("React with 🎉"));
    await waitFor(() =>
      expect(mockSetReaction).toHaveBeenCalledWith("room-1", "m2", "🎉", true),
    );
    // Acting dismisses the sheet, the way Slack's does.
    expect(screen.queryByText("Mark unread")).toBeNull();
    // Mark unread FROM that message's sequence, not the whole room from zero.
    await fireEvent(screen.getByText("second"), "longPress");
    await fireEvent.press(screen.getByText("Mark unread"));
    await waitFor(() => expect(mockMarkUnread).toHaveBeenCalledWith("room-1", 2));
  });

  // The bug this task exists to fix: both screens used to swallow
  // `setReaction`'s rejection with `.catch(() => {})`, so a failed reaction
  // looked like nothing happened. This exercises the real `reactToMessage`
  // path (not just the presentational `reactionError` prop) end to end, and
  // covers the unconditional-clear timing: a rejection on one message must
  // not survive a SUCCESSFUL toggle on a different one.
  it("surfaces a failed reaction toggle and clears it on a later success elsewhere", async () => {
    mockSetReaction.mockRejectedValueOnce(new Error("offline"));
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    await fireEvent(screen.getByText("second"), "longPress");
    await fireEvent.press(screen.getByLabelText("React with 🎉"));
    await waitFor(() =>
      expect(screen.getByText("Could not update reaction. Try again.")).toBeTruthy(),
    );
    // A successful toggle on a DIFFERENT message (m1, "first") still clears it.
    await fireEvent(screen.getByText("first"), "longPress");
    await fireEvent.press(screen.getByLabelText("React with 🎉"));
    await waitFor(() =>
      expect(screen.queryByText("Could not update reaction. Try again.")).toBeNull(),
    );
  });

  it("opens the thread from the sheet's Reply in thread", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    await fireEvent(screen.getByText("second"), "longPress");
    await fireEvent.press(screen.getByText("Reply in thread"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1/thread/m2");
  });

  it("shows a jump-to-latest pill only after scrolling away from the bottom", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    expect(screen.queryByLabelText("Jump to latest")).toBeNull();
    await fireEvent.scroll(screen.getByTestId("room-timeline"), {
      nativeEvent: {
        contentOffset: { y: 0, x: 0 },
        contentSize: { height: 2000, width: 400 },
        layoutMeasurement: { height: 600, width: 400 },
      },
    });
    expect(screen.getByLabelText("Jump to latest")).toBeTruthy();
  });

  // Nothing wrote the push handler's target before this task: the signal existed
  // and no screen set it, so the in-app banner was never suppressed.
  it("claims the push target while on screen and releases it on blur", async () => {
    await render(<Conversation />);
    expect(getCurrentTarget()).toMatchObject({ roomId: "room-1", threadId: null });
    await act(async () => mockFocus.blur?.());
    expect(getCurrentTarget()).toMatchObject({ roomId: null, threadId: null });
  });

  // Mirrors web's RoomPanel `room-blocked` guard (search `room-blocked`):
  // the whole panel is replaced, not banner-over-composer.
  describe("blocked states", () => {
    it("shows the loading state with neither timeline nor composer while the room is loading", async () => {
      mockRoomId = "room-loading";
      mockLive.messaging.getRoom.mockImplementationOnce(() => new Promise(() => {}));
      await render(<Conversation />);
      expect(screen.getByText("Loading conversation…")).toBeTruthy();
      expect(screen.queryByTestId("room-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText("Message")).toBeNull();
    });

    it("shows the access-removed state with no composer and no retry once access was rotated away", async () => {
      mockRoomId = "room-access-removed";
      mockLive.messaging.getRoom.mockRejectedValueOnce(new ApiError(403, null));
      await render(<Conversation />);
      await waitFor(() =>
        expect(
          screen.getByText("Access removed. You can no longer view this conversation."),
        ).toBeTruthy(),
      );
      expect(screen.queryByTestId("room-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText("Message")).toBeNull();
      expect(screen.queryByText("Try again")).toBeNull();
    });

    it("shows the unavailable state, and Try again recovers the room", async () => {
      mockRoomId = "room-unavailable";
      mockLive.messaging.getRoom.mockRejectedValueOnce(new Error("boom"));
      await render(<Conversation />);
      await waitFor(() =>
        expect(screen.getByText("This conversation is unavailable.")).toBeTruthy(),
      );
      expect(screen.queryByTestId("room-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText("Message")).toBeNull();
      await fireEvent.press(screen.getByText("Try again"));
      await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
      expect(screen.getByPlaceholderText("Message")).toBeTruthy();
    });
  });

  it("reports typing to the room as the draft changes, and stops on blur", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Message"), "typing now");
    await waitFor(() =>
      expect(mockSetTyping).toHaveBeenCalledWith({ roomId: "room-1", isTyping: true }),
    );
    mockSetTyping.mockClear();
    await fireEvent(screen.getByPlaceholderText("Message"), "blur");
    await waitFor(() =>
      expect(mockSetTyping).toHaveBeenCalledWith({ roomId: "room-1", isTyping: false }),
    );
  });

  it("shows another member's typing line with the web's exact wording", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    expect(screen.queryByText(/typing…/)).toBeNull();
    const expiresAt = new Date(Date.now() + 5_000).toISOString();
    await act(async () => {
      for (const handler of typingHandlers) {
        handler({
          type: "room.typing.changed",
          payload: { roomId: "room-1", userId: "user-b", displayName: "Bob", expiresAt },
        } as LiveEvent);
      }
    });
    await waitFor(() => expect(screen.getByText("Bob is typing…")).toBeTruthy());
  });

  it("stars and mutes the room from the header, inverting the room's current state", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByLabelText("Star this conversation")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Star this conversation"));
    await waitFor(() => expect(mockSetMemberPrefs).toHaveBeenCalledWith("room-1", { starred: true }));
    await fireEvent.press(screen.getByLabelText("Mute this conversation"));
    await waitFor(() => expect(mockSetMemberPrefs).toHaveBeenCalledWith("room-1", { muted: true }));
  });

  it("shows the web's copy when the header's star save fails", async () => {
    mockSetMemberPrefs.mockRejectedValueOnce(new Error("offline"));
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByLabelText("Star this conversation")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Star this conversation"));
    await waitFor(() => expect(screen.getByText("The star could not be saved.")).toBeTruthy());
  });

  it("shows the devices header action, badges live sessions, and pushes the devices screen", async () => {
    mockListResources.mockResolvedValue({ devices: [{ deviceId: "mac-1", humanUserId: "user-a", cliDeviceId: null }], sessions: [mockSession], pairedNodes: [] });
    await render(<Conversation />);
    const button = await screen.findByLabelText("Devices & sessions, 1 live");
    await fireEvent.press(button);
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1/devices");
  });
});
