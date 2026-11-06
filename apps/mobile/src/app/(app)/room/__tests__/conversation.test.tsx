import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import {
  ApiError,
  dateDividerLabel,
  type CanonicalRoomMessage,
  type LiveEvent,
  type RoomDetail,
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
// Whether navigation considers the screen focused. Upstream, a dep change
// re-subscribes the focus effect but only RUNS it on a focused screen — which
// is the difference between a peek the reader opened and one they dismissed,
// so the mock has to carry it. Defaults to focused; reset in beforeEach.
let mockFocused = true;
// The blocked-state tests each need their own SWR cache entry (the shared
// room machine's `useSWR(["messaging-room", scope, activeRoomId], ...)` is
// keyed off this), so a getRoom override in one test can't be served from
// another test's cached "room-1" success. Reset in beforeEach.
let mockRoomId = "room-1";
let mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
// A long-press peek from the room list renders this same screen (Link.Preview).
let mockIsPreview = false;
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
    // Real react-navigation renders `options.headerTitle`/`headerRight` inside
    // the native header; this mock is the whole navigator here, so it has to
    // do that itself or the header (and its star/mute buttons) would never
    // mount at all.
    Stack: {
      Screen: ({ options }: { options?: { headerTitle?: () => ReactNode; headerRight?: () => ReactNode } }) => (
        <>
          {options?.headerTitle ? options.headerTitle() : null}
          {options?.headerRight ? options.headerRight() : null}
        </>
      ),
    },
    useFocusEffect: (cb: () => undefined | (() => void)) =>
      useEffect(() => {
        let cleanup = mockFocused ? cb() : undefined;
        mockFocus.blur = () => {
          mockFocused = false;
          cleanup?.();
          cleanup = undefined;
        };
        mockFocus.focus = () => {
          mockFocused = true;
          cleanup = cb();
        };
        return () => cleanup?.();
      }, [cb]),
    useLocalSearchParams: () => ({ roomId: mockRoomId }),
    useIsPreview: () => mockIsPreview,
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
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: { id: `org-${mockScope}` } }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => mockInsets,
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
// A room-turn reply to "mine" (m3) -- Friday's run started on it, so read
// state moves from "Sent" to "Read by N" once this lands.
const agentReply: CanonicalRoomMessage = {
  messageId: "m-agent", roomId: "room-1", messageSeq: 4, roomRevision: 1,
  author: { kind: "agent", id: "agent-1", displayName: "Friday" }, text: "on it",
  mentions: [], replyToMessageId: null, threadRootMessageId: null,
  createdAt: "2026-06-11T10:05:00Z", runId: "run-9",
};

const mockListSubagents = jest.fn(async () => ({ subagents: [] }));
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
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null,
  // The front agent (`@lightbulb`) as the live provider resolves it from the agents list.
  frontAgentId: "agent-lb",
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
    listMessages: jest.fn(async (roomId: string, _afterSeq?: number, _limit?: number, _options?: { beforeSeq?: number }) => ({
      messages: mockMessages.map((message) => ({ ...message, roomId })),
      nextAfterSeq: null,
    })),
    markRead: mockMarkRead,
    sendMessage: mockSendMessage,
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    downloadAttachment: jest.fn(), attachmentUrl: jest.fn(), uploadAttachment: jest.fn(), markUnread: mockMarkUnread,
    setTyping: mockSetTyping, abortRun: jest.fn(), steerRun: jest.fn(), listSubagents: mockListSubagents, subagentTail: jest.fn(), steerSubagent: jest.fn(), stopSubagent: jest.fn(), createRoom: jest.fn(),
    updateRoom: jest.fn(), upsertMember: jest.fn(), removeMember: jest.fn(),
    updateMessage: jest.fn(), setReaction: mockSetReaction, getLiveConfig: jest.fn(),
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
let mockSuperAgent = false;
let mockContainer: { kind: string } | null = null;
jest.mock("../../../../lib/containerStatusContext", () => ({ useContainerStatusContext: () => mockContainer }));
jest.mock("../../../../providers/live-events", () => ({ useSuperAgent: () => mockSuperAgent, useLiveEvents: () => mockLive, useOptionalLiveEvents: () => mockLive }));
// The front agent (`@lightbulb`) once the roster answers; only asked for while the switch is on.
// The band has its own suite; here only WHERE it mounts is under test.
jest.mock("../../../../components/room/OnboardingBand", () => ({
  OnboardingBand: () => {
    const { Text } = require("react-native");
    return <Text>onboarding-band</Text>;
  },
}));
// The header now calls this (a human DM's "online" status); it has its own
// suite, and its heartbeat/snapshot timers have no place in this one.
jest.mock("../../../../lib/presence", () => ({ usePresence: () => new Set<string>() }));

// Live: the binary has WebRTC; a test sets who is Live, whether Live is on, and whether this phone is in one.
let mockRoomLive = { userIds: [] as string[], available: false };
let mockLiveState: { room: object | null; roomId: string | null; roomName: string | null; joining: string | null; open: boolean; notice: object | null; localIdentity: string | null } = { room: null, roomId: null, roomName: null, joining: null, open: false, notice: null, localIdentity: "user-a" };
const mockJoinLive = jest.fn();
const mockDismissNotice = jest.fn();
const mockOpenLive = jest.fn();
jest.mock("../../../../lib/webrtc", () => ({ hasNativeVoice: () => true }));
jest.mock("../../../../providers/live", () => ({
  useRoomLive: () => mockRoomLive,
  useLive: () => ({ workerAttrs: {}, roster: [], ...mockLiveState, dismissNotice: mockDismissNotice, join: (...a: unknown[]) => mockJoinLive(...a), setOpen: (...a: unknown[]) => mockOpenLive(...a) }),
}));

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
    mockInsets = { top: 0, bottom: 0, left: 0, right: 0 };
    mockScope += 1;
    mockPush.mockClear();
    mockMarkRead.mockClear();
    mockListSubagents.mockClear();
    mockSendMessage.mockClear();
    mockMarkUnread.mockClear();
    mockSetReaction.mockClear();
    mockSetTyping.mockClear();
    typingHandlers.clear();
    mockSetMemberPrefs.mockClear();
    mockJoinLive.mockClear();
    mockOpenLive.mockClear();
    mockMessages = [...mockBase];
    mockRoomId = "room-1";
    mockSuperAgent = false;
    mockContainer = null;
    mockIsPreview = false;
    mockFocused = true;
    mockLive.messaging.listRooms.mockReset().mockImplementation(async () => ({ rooms: [mockRoom] }));
    mockLive.messaging.getRoom.mockReset().mockImplementation(async (roomId: string) =>
      roomId === "dm-friday"
        ? { ...mockRoom, roomId, kind: "dm" as const, name: null, lastMessageSeq: 0 }
        : { ...mockRoom, roomId },
    );
    mockLive.messaging.listMessages.mockReset().mockImplementation(async (roomId: string) => ({
      messages: mockMessages.map((message) => ({ ...message, roomId })), nextAfterSeq: null,
    }));
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

  it("mounts the onboarding band in the starter channel only", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    expect(screen.queryByText("onboarding-band")).toBeNull();
  });

  it("opens the member picker directly from an owned channel header", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByLabelText("Add people")).toBeTruthy());

    await fireEvent.press(screen.getByLabelText("Add people"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1/details?addPeople=1");
  });

  it.each([
    ["ordinary members", { members: mockRoom.members.map((member) => member.id === "user-a" ? { ...member, role: "member" as const } : member) }],
    ["shared channels", { shared: true }],
    ["group DMs", { kind: "group-dm" as const }],
  ])("hides the direct member picker action for %s", async (_label, roomPatch) => {
    mockLive.messaging.getRoom.mockImplementation(async (roomId: string) => ({ ...mockRoom, ...roomPatch, roomId }));
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    expect(screen.queryByLabelText("Add people")).toBeNull();
  });

  it("mounts the onboarding band when the room is the workspace default", async () => {
    mockRoomId = "room-default";
    mockLive.messaging.getRoom.mockImplementation(async (roomId: string) => ({ ...mockRoom, roomId, isDefault: true }));
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("onboarding-band")).toBeTruthy());
  });

  it("reads an archived room without a composer (an ended session's chat)", async () => {
    mockLive.messaging.getRoom.mockImplementation(async (roomId: string) => ({ ...mockRoom, roomId, archivedAt: "2026-10-07T11:00:00Z", session: { sessionId: "s1" } }));
    await render(<Conversation />);
    expect(await screen.findByText("This session ended. Its chat is read-only.")).toBeTruthy();
    expect(screen.queryByPlaceholderText(/^Message /)).toBeNull();
  });

  it("draws the New messages line at the read cursor the room opened with", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("New messages")).toBeTruthy());
  });

  // It used to sit above the reader's own message: the first one past the
  // cursor they opened with.
  it("drops the New messages line once the reader sends", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("New messages")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText(/^Message/), "caught up");
    await fireEvent.press(screen.getByLabelText("Send"));
    await waitFor(() => expect(screen.queryByText("New messages")).toBeNull());
  });

  it("exposes the Summary action without posting into conversation history", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByLabelText("Summary")).toBeTruthy());

    await fireEvent.press(screen.getByLabelText("Summary"));
    await waitFor(() => expect(screen.getByText("Catch-up summary")).toBeTruthy());
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it("marks the room read up to the newest sequence once it is on screen", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(mockMarkRead).toHaveBeenCalledWith("room-1", { lastReadSeq: 3 }));
  });

  // A peek is a look, not a visit: the room list's Mark unread sits right under
  // it in the same menu, and a peek that marked the room read would undo it.
  it("shows the conversation in a peek without marking it read or claiming the push target", async () => {
    mockIsPreview = true;
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    await act(async () => { await Promise.resolve(); });
    expect(mockMarkRead).not.toHaveBeenCalled();
    expect(getCurrentTarget()).toMatchObject({ roomId: null, threadId: null });
    expect(mockListSubagents).not.toHaveBeenCalled();
  });

  // A peek is also not a subscription. Dismissing one leaves this route
  // preloaded and unfocused for the rest of the session, and a room it still
  // held selected would poll its detail every 60s from then on — once per row
  // ever peeked. So the dismissed peek lets the room go; opening it properly
  // selects it again, off the history already cached.
  it("lets the room go when a peek is dismissed without being opened", async () => {
    // Fake timers only here: the assertion is about a 60s poll not running.
    jest.useFakeTimers({ doNotFake: ["performance"] });
    mockIsPreview = true;
    const view = await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());

    // The peek is dismissed: this route stays mounted, and unfocused.
    mockFocused = false;
    mockIsPreview = false;
    await view.rerender(<Conversation />);
    await act(async () => { await Promise.resolve(); });
    const settled = mockLive.messaging.getRoom.mock.calls.length;
    await act(async () => { jest.advanceTimersByTime(180_000); });
    // Three minutes of a 60s poll is three fetches, if it is still running.
    expect(mockLive.messaging.getRoom).toHaveBeenCalledTimes(settled);

    // Opening it for real takes the room back, so it polls again.
    await act(async () => mockFocus.focus?.());
    await waitFor(() => expect(mockLive.messaging.getRoom).toHaveBeenCalled());
    jest.useRealTimers();
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
    await fireEvent.changeText(screen.getByPlaceholderText(/^Message/), "will fail");
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

  // The inverted list hands each row the flip that rights it through the row's
  // own `style` prop. EmptyState takes no `style`, so the empty state drew
  // upside down until a wrapper took the flip for it.
  it("draws the empty state the right way up inside the inverted list", async () => {
    mockMessages = [];
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("No messages yet")).toBeTruthy());
    const empty = screen.getByTestId("room-timeline").props.ListEmptyComponent;
    expect(empty.props.style).toMatchObject({ transform: [{ scaleY: -1 }] });
  });

  it("keeps the keyboard dismissable by drag and taps landing on controls", async () => {
    await render(<Conversation />);
    const list = screen.getByTestId("room-timeline");
    expect(list.props.keyboardDismissMode).toBe("interactive");
    expect(list.props.keyboardShouldPersistTaps).toBe("handled");
  });

  it("uses a deterministic header offset so the composer stays above the keyboard", async () => {
    mockInsets = { top: 59, bottom: 34, left: 0, right: 0 };
    await render(<Conversation />);

    // The timeline sits in a box of its own (with the way back to its newest message).
    const keyboardAvoider = screen.getByTestId("room-timeline").parent?.parent;
    expect(keyboardAvoider?.props.automaticOffset).toBeUndefined();
    // Status bar 59 + nav bar 44 - home indicator 34, plus 8 of air over the keyboard.
    expect(keyboardAvoider?.props.keyboardVerticalOffset).toBe(77);
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

  // On an inverted list offset 0 IS the newest message, so the pill is purely a
  // function of how far back the reader has scrolled — no content/layout maths.
  it("shows a jump-to-latest pill only after scrolling away from the bottom", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    expect(screen.queryByLabelText("Jump to latest")).toBeNull();
    const scrollTo = (y: number) =>
      fireEvent.scroll(screen.getByTestId("room-timeline"), {
        nativeEvent: {
          contentOffset: { y, x: 0 },
          contentSize: { height: 2000, width: 400 },
          layoutMeasurement: { height: 600, width: 400 },
        },
      });
    // Still within the slack the list itself follows in: the reader has not really left the bottom.
    await scrollTo(40);
    expect(screen.queryByLabelText("Jump to latest")).toBeNull();
    await scrollTo(400);
    expect(screen.getByLabelText("Jump to latest")).toBeTruthy();
    await scrollTo(0);
    expect(screen.queryByLabelText("Jump to latest")).toBeNull();
  });

  it("counts what arrived from others while the reader was scrolled back", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    // Just past where the list stops following: left, by the same number.
    const list = screen.getByTestId("room-timeline");
    const follows = list.props.maintainVisibleContentPosition.autoscrollToTopThreshold;
    await fireEvent.scroll(list, {
      nativeEvent: { contentOffset: { y: follows + 1, x: 0 }, contentSize: { height: 2000, width: 400 }, layoutMeasurement: { height: 600, width: 400 } },
    });
    expect(screen.getByLabelText("Jump to latest")).toBeTruthy();
    // Two arrive: one from someone else, one of the reader's own from another device.
    const arrives = (message: CanonicalRoomMessage) => act(async () => {
      for (const handler of typingHandlers) {
        handler({ schemaVersion: 1, eventId: `e-${message.messageSeq}`, type: "room.message.created", occurredAt: message.createdAt, roomId: "room-1", messageSeq: message.messageSeq, roomRevision: 1, payload: message } as LiveEvent);
      }
    });
    await arrives(agentReply);
    await arrives(mockMsg({ messageId: "m5", messageSeq: 5, text: "mine, elsewhere", author: { kind: "human", id: "user-a", displayName: "Alice" } }));
    await waitFor(() => expect(screen.getByLabelText("Jump to latest, 1 new message")).toBeTruthy());
  });

  // The whole point of the inversion: the newest message is simply THERE at
  // offset 0, so nothing scrolls itself when a message, a run frame or the
  // keyboard changes the list's size. `buildTimeline` hands us oldest → newest,
  // so index 0 of the list's data must be the NEWEST item.
  it("draws the timeline inverted, newest item first, with no self-scrolling", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    const list = screen.getByTestId("room-timeline");
    expect(list.props.inverted).toBe(true);
    const data = list.props.data as { key: string; message?: { text: string } }[];
    expect(data[0]?.message?.text).toBe("mine");
    // Oldest item last — the date divider that opens the day, not a message.
    expect(data.at(-1)?.message).toBeUndefined();
  });

  it("opens at the newest page and loads older messages without losing the visible row", async () => {
    const server = Array.from({ length: 201 }, (_, index) => mockMsg({
      messageId: `m${index + 1}`, messageSeq: index + 1, text: `message ${index + 1}`,
    }));
    mockLive.messaging.listRooms.mockResolvedValue({ rooms: [{ ...mockRoom, lastMessageSeq: 201 }] });
    mockLive.messaging.getRoom.mockResolvedValue({ ...mockRoom, lastMessageSeq: 201 });
    mockLive.messaging.listMessages.mockImplementation(async (_roomId, afterSeq = 0, limit = 100, options) => {
      if (options?.beforeSeq !== undefined) {
        const messages = server.filter((item) => item.messageSeq < options.beforeSeq!).slice(-limit);
        return { messages, nextAfterSeq: null, nextBeforeSeq: messages[0]?.messageSeq > 1 ? messages[0].messageSeq : null };
      }
      return { messages: server.filter((item) => item.messageSeq > afterSeq).slice(0, limit), nextAfterSeq: null };
    });
    await render(<Conversation />);
    await waitFor(() => expect((screen.getByTestId("room-timeline").props.data as { message?: CanonicalRoomMessage }[])[0]?.message?.messageSeq).toBe(201));
    expect((screen.getByTestId("room-timeline").props.data as { message?: CanonicalRoomMessage }[]).some((item) => item.message?.messageSeq === 1)).toBe(false);
    const list = screen.getByTestId("room-timeline");
    // FlatList counts ListHeaderComponent as index 0 before forwarding this to ScrollView.
    expect(list.props.maintainVisibleContentPosition).toEqual({ minIndexForVisible: 1, autoscrollToTopThreshold: 48 });
    expect(mockLive.messaging.listMessages).toHaveBeenCalledWith("room-1", 0, 100, expect.objectContaining({ beforeSeq: 202 }));
    // Nothing to tap: reaching the top reads older history, page after page
    // while the reader stays there, until the room's first message is in.
    expect(screen.queryByLabelText("Load older messages")).toBeNull();
    await act(async () => {
      list.props.onScroll?.({
        nativeEvent: { contentOffset: { x: 0, y: 1200 }, contentSize: { width: 390, height: 2000 }, layoutMeasurement: { width: 390, height: 800 } },
      });
    });
    await waitFor(() => expect((screen.getByTestId("room-timeline").props.data as { message?: CanonicalRoomMessage }[]).some((item) => item.message?.messageSeq === 2)).toBe(true));
    await waitFor(() => expect((screen.getByTestId("room-timeline").props.data as { message?: CanonicalRoomMessage }[]).some((item) => item.message?.messageSeq === 1)).toBe(true));
    expect(mockLive.messaging.listMessages).toHaveBeenCalledWith("room-1", 0, 100, expect.objectContaining({ beforeSeq: 102 }));
    expect(mockLive.messaging.listMessages).toHaveBeenCalledWith("room-1", 0, 100, expect.objectContaining({ beforeSeq: 2 }));
  });

  it("does not call a room empty when its newest page contains only thread replies", async () => {
    const server = Array.from({ length: 201 }, (_, index) => mockMsg({
      messageId: `m${index + 1}`, messageSeq: index + 1,
      threadRootMessageId: index === 0 ? null : "m1",
      text: `message ${index + 1}`,
    }));
    mockLive.messaging.listRooms.mockResolvedValue({ rooms: [{ ...mockRoom, lastMessageSeq: 201 }] });
    mockLive.messaging.getRoom.mockResolvedValue({ ...mockRoom, lastMessageSeq: 201 });
    mockLive.messaging.listMessages.mockImplementation(async (_roomId, afterSeq = 0, limit = 100, options) => {
      if (options?.beforeSeq !== undefined) {
        const messages = server.filter((item) => item.messageSeq < options.beforeSeq!).slice(-limit);
        return { messages, nextAfterSeq: null, nextBeforeSeq: messages[0]?.messageSeq > 1 ? messages[0].messageSeq : null };
      }
      return { messages: server.filter((item) => item.messageSeq > afterSeq).slice(0, limit), nextAfterSeq: null };
    });
    await render(<Conversation />);
    await waitFor(() => expect(mockLive.messaging.listMessages).toHaveBeenCalledWith("room-1", 0, 100, expect.objectContaining({ beforeSeq: 202 })));
    expect(screen.queryByText("No messages yet")).toBeNull();
    // An empty screen with history behind it reads back on its own until
    // something draws: here the thread's root, the room's first message.
    const list = screen.getByTestId("room-timeline");
    await act(async () => {
      list.props.onLayout?.({ nativeEvent: { layout: { x: 0, y: 0, width: 390, height: 800 } } });
    });
    // ponytail: waitFor's 1 s default is too tight here — this read-back pages
    // and renders two 100-message pages (~0.9 s cold locally, flaky on the Linux
    // runner). Shrink the fixture instead if the page size ever becomes injectable.
    await waitFor(() => expect(screen.getByText("message 1")).toBeTruthy(), { timeout: 4000 });
    expect(screen.queryByText("No messages yet")).toBeNull();
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
    it("paints the timeline and composer from the room list while the detail loads, and subscribes only with the detail's key", async () => {
      let resolveDetail: (room: RoomDetail) => void = () => {};
      mockLive.messaging.getRoom.mockImplementationOnce(
        () => new Promise<RoomDetail>((resolve) => { resolveDetail = resolve; }),
      );
      mockLive.claimActiveRoomSubscription.mockClear();
      await render(<Conversation />);
      await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
      expect(screen.queryByText("Loading chat…")).toBeNull();
      expect(screen.getByPlaceholderText(/^Message/)).toBeTruthy();
      // The list carries no deliveryKey: nothing may subscribe on a guess.
      expect(mockLive.claimActiveRoomSubscription).not.toHaveBeenCalled();

      await act(async () => resolveDetail({ ...mockRoom, deliveryKey: "detail-key" }));
      await waitFor(() =>
        expect(mockLive.claimActiveRoomSubscription).toHaveBeenCalledWith(
          expect.objectContaining({ roomId: "room-1", deliveryKey: "detail-key" }),
        ),
      );
    });

    it("shows the loading state with neither timeline nor composer while the room is loading", async () => {
      mockRoomId = "room-loading";
      mockLive.messaging.getRoom.mockImplementationOnce(() => new Promise(() => {}));
      await render(<Conversation />);
      expect(screen.getByText("Loading chat…")).toBeTruthy();
      expect(screen.queryByTestId("room-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText(/^Message/)).toBeNull();
    });

    it("shows the access-removed state with no composer and no retry once access was rotated away", async () => {
      mockRoomId = "room-access-removed";
      mockLive.messaging.getRoom.mockRejectedValueOnce(new ApiError(403, null));
      await render(<Conversation />);
      await waitFor(() =>
        expect(
          screen.getByText("Access removed. You can no longer view this chat."),
        ).toBeTruthy(),
      );
      expect(screen.queryByTestId("room-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText(/^Message/)).toBeNull();
      expect(screen.queryByText("Try again")).toBeNull();
    });

    it("shows the unavailable state, and Try again recovers the room", async () => {
      mockRoomId = "room-unavailable";
      mockLive.messaging.getRoom.mockRejectedValueOnce(new Error("boom"));
      await render(<Conversation />);
      await waitFor(() =>
        expect(screen.getByText("This chat is unavailable.")).toBeTruthy(),
      );
      expect(screen.queryByTestId("room-timeline")).toBeNull();
      expect(screen.queryByPlaceholderText(/^Message/)).toBeNull();
      await fireEvent.press(screen.getByText("Try again"));
      await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
      expect(screen.getByPlaceholderText(/^Message/)).toBeTruthy();
    });
  });

  it("reports typing to the room as the draft changes, and stops on blur", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText(/^Message/), "typing now");
    await waitFor(() =>
      expect(mockSetTyping).toHaveBeenCalledWith({ roomId: "room-1", isTyping: true }),
    );
    mockSetTyping.mockClear();
    await fireEvent(screen.getByPlaceholderText(/^Message/), "blur");
    await waitFor(() =>
      expect(mockSetTyping).toHaveBeenCalledWith({ roomId: "room-1", isTyping: false }),
    );
  });

  it("shows another member's typing line with the web's exact wording", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    expect(screen.queryByLabelText(/typing…/)).toBeNull();
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

  it("stars and mutes the room from the header, inverting the room's current state", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByLabelText("Star")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Star"));
    await waitFor(() => expect(mockSetMemberPrefs).toHaveBeenCalledWith("room-1", { starred: true }));
    await fireEvent.press(screen.getByLabelText("Mute"));
    await waitFor(() => expect(mockSetMemberPrefs).toHaveBeenCalledWith("room-1", { muted: true }));
  });

  it("shows the web's copy when the header's star save fails", async () => {
    mockSetMemberPrefs.mockRejectedValueOnce(new Error("offline"));
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByLabelText("Star")).toBeTruthy());
    await fireEvent.press(screen.getByLabelText("Star"));
    await waitFor(() => expect(screen.getByText("The star could not be saved.")).toBeTruthy());
  });

  it("super-agent on: a specialist's DM says it now works through Lightbulb; off, no banner", async () => {
    mockRoomId = "dm-friday";
    const off = await render(<Conversation />);
    await screen.findByLabelText("Friday, idle");
    expect(screen.queryByTestId("specialist-dm-banner")).toBeNull();
    await off.unmount();
    mockSuperAgent = true;
    await render(<Conversation />);
    expect(await screen.findByText("To ask Friday for something, mention them in Lightbulb\u2019s chat.")).toBeTruthy();
  });

  it("super-agent on: the front agent reads Lightbulb in the header and on its older posts, whatever its seat says", async () => {
    mockSuperAgent = true;
    mockLive.frontAgentId = "agent-1"; // Friday stands in for the front agent here
    try {
      mockRoomId = "dm-friday";
      await render(<Conversation />);
      expect(await screen.findByLabelText("Lightbulb, idle")).toBeTruthy();
      // Its older posts (stored as "Friday") read Lightbulb too, avatar and byline from one name.
      expect((await screen.findAllByText("Lightbulb")).length).toBeGreaterThan(0);
      expect(screen.queryByText("Friday")).toBeNull();
      expect(screen.queryByTestId("specialist-dm-banner")).toBeNull();
    } finally { mockLive.frontAgentId = "agent-lb"; }
  });

  it("super-agent, setup still running: my unanswered message reads Queued; once ready it reads as before", async () => {
    mockSuperAgent = true;
    mockContainer = { kind: "starting" };
    const view = await render(<Conversation />);
    expect(await screen.findByText("Queued · Lightbulb answers as soon as setup finishes")).toBeTruthy();
    await view.unmount();
    mockContainer = { kind: "ready" };
    await render(<Conversation />);
    await screen.findByText("mine");
    expect(screen.queryByText(/^Queued/)).toBeNull();
  });

  it("titles an agent DM with the agent and its idle status, opening details", async () => {
    mockRoomId = "dm-friday";
    // The suite's getRoom mock returns this detail for "dm-friday": kind "dm", members [Alice (viewer), Friday (agent)].
    await render(<Conversation />);
    await fireEvent.press(await screen.findByLabelText("Friday, idle"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/dm-friday/details");
  });

  it("keeps a run's background subagents under the saved reply once the run card is gone", async () => {
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("second")).toBeTruthy());
    const emit = (event: LiveEvent) => act(async () => { for (const handler of typingHandlers) handler(event); });
    const frame = (body: Record<string, unknown>) => ({ type: "room.run.frame",
      payload: { roomId: "room-1", agentId: "agent-1", runId: "run-9", threadRootMessageId: null, frame: body } } as LiveEvent);
    await emit(frame({ type: "tool_start", runId: "run-9", tool: "delegate_task", toolCallId: "t1", args: {} }));
    await emit(frame({ type: "tool_end", runId: "run-9", tool: "delegate_task", toolCallId: "t1", result: JSON.stringify({ delegation_id: "d-1" }) }));
    await emit({ schemaVersion: 1, eventId: "e-9", type: "room.message.created", occurredAt: agentReply.createdAt, roomId: "room-1", messageSeq: 4, roomRevision: 1, payload: agentReply } as LiveEvent);
    await emit(frame({ type: "run_ended", outcome: "done" }));
    await waitFor(() => expect(screen.getByText("on it")).toBeTruthy());
    expect(screen.getByText("Background subagents dispatched")).toBeTruthy();
  });

  it("shows Sent under my last message until someone answers, then Read by", async () => {
    // A group case (a channel, not a DM), and readState's "recipient" check
    // needs the reader to actually be a member, so Bob joins this room's fixture.
    mockLive.messaging.getRoom.mockImplementation(async (roomId: string) => ({
      ...mockRoom, roomId,
      members: [...mockRoom.members, { kind: "human" as const, id: "user-b", displayName: "Bob", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "x", joinedSeq: 0 }],
    }));
    await render(<Conversation />);
    // Fixture: channel "room-1" whose newest message (seq 3) is Alice's (the viewer), nothing after it.
    await waitFor(() => expect(screen.getByText("Sent")).toBeTruthy());
    await act(async () => {
      for (const handler of typingHandlers) {
        handler({ schemaVersion: 1, eventId: "e-9", type: "room.message.created", occurredAt: "2026-06-11T10:05:00Z", roomId: "room-1", messageSeq: 4, roomRevision: 1, payload: agentReply } as LiveEvent);
      }
    });
    await waitFor(() => expect(screen.getByText("Read by 1")).toBeTruthy());
  });

  it("moves my last message to Read by 2 when a member's cursor passes it", async () => {
    mockLive.messaging.getRoom.mockImplementation(async (roomId: string) => ({
      ...mockRoom, roomId,
      members: [...mockRoom.members, { kind: "human" as const, id: "user-b", displayName: "Bob", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "x", joinedSeq: 0 }],
    }));
    await render(<Conversation />);
    await waitFor(() => expect(screen.getByText("Sent")).toBeTruthy());
    await act(async () => {
      for (const handler of typingHandlers) {
        handler({ schemaVersion: 1, eventId: "e-9", type: "room.message.created", occurredAt: "2026-06-11T10:05:00Z", roomId: "room-1", messageSeq: 4, roomRevision: 1, payload: agentReply } as LiveEvent);
        handler({ schemaVersion: 1, eventId: "e-10", type: "room.read.changed", occurredAt: "2026-06-11T10:06:00Z", roomId: "room-1", payload: { roomId: "room-1", reader: { kind: "human", id: "user-b" }, lastReadSeq: 4 } } as LiveEvent);
      }
    });
    await waitFor(() => expect(screen.getByText("Read by 2")).toBeTruthy());
  });


  it("offers Go live, not Talk, in an agent DM", async () => {
    mockRoomId = "dm-friday";
    mockRoomLive = { userIds: [], available: true };
    await render(<Conversation />);
    await fireEvent.press(await screen.findByLabelText("Go live"));
    expect(mockJoinLive).toHaveBeenCalledWith("dm-friday", "Friday");
    expect(screen.queryByLabelText("Talk to Friday")).toBeNull();
    mockRoomLive = { userIds: [], available: false };
  });

  it("while Live elsewhere, the pill reopens that Live instead of joining", async () => {
    mockRoomLive = { userIds: [], available: true };
    mockLiveState = { ...mockLiveState, room: {}, roomId: "room-other", roomName: "#ops", joining: null };
    await render(<Conversation />);
    await fireEvent.press(await screen.findByLabelText("Open Live in #ops"));
    expect(mockOpenLive).toHaveBeenCalledWith(true);
    expect(mockJoinLive).not.toHaveBeenCalled();
    mockLiveState = { room: null, roomId: null, roomName: null, joining: null, open: false, notice: null, localIdentity: "user-a" };
    mockRoomLive = { userIds: [], available: false };
  });

  it("labels the Live button with the speaking agent, only in this room", async () => {
    const roster = [{ id: "gauge", kind: "agent", displayName: "Gauge" }];
    const workerAttrs = { "lightbulb.speaking_agent": "gauge" };
    mockLiveState = { ...mockLiveState, room: {}, roomId: "room-1", roomName: "#x", roster, workerAttrs } as typeof mockLiveState;
    const view = await render(<Conversation />);
    expect(await screen.findByLabelText("Open Live, Gauge speaking")).toBeTruthy();
    mockLiveState = { ...mockLiveState, roomId: "room-other", roomName: "#ops" };
    await view.rerender(<Conversation />);
    expect(await screen.findByLabelText("Open Live in #ops")).toBeTruthy();
    mockLiveState = { room: null, roomId: null, roomName: null, joining: null, open: false, notice: null, localIdentity: "user-a" };
  });

  it("shows a Live rename banner on the room screen only while the sheet is hidden", async () => {
    const renamed = { kind: "renamed", agentId: "a", from: "CEO", to: "Nova", by: "user-b", collides: false, at: 1 };
    mockLiveState = { ...mockLiveState, room: {}, roomId: "room-1", roomName: "#x", notice: renamed, open: false };
    const view = await render(<Conversation />);
    expect(await screen.findByText("CEO is now Nova")).toBeTruthy();
    mockLiveState = { ...mockLiveState, open: true };
    await view.rerender(<Conversation />);
    expect(screen.queryByText("CEO is now Nova")).toBeNull();
    mockLiveState = { room: null, roomId: null, roomName: null, joining: null, open: false, notice: null, localIdentity: "user-a" };
  });

  it("offers Go live in a channel only when Live is on, and names who is Live", async () => {
    mockRoomLive = { userIds: [], available: false };
    const first = await render(<Conversation />);
    await waitFor(() => expect(screen.getByLabelText("Star")).toBeTruthy());
    expect(screen.queryByLabelText("Go live")).toBeNull();
    await first.unmount();

    mockRoomLive = { userIds: ["user-a"], available: true };
    await render(<Conversation />);
    await fireEvent.press(await screen.findByLabelText("Go live"));
    expect(mockJoinLive).toHaveBeenCalledWith("room-1", "Launch");
    expect(screen.getByText("Live — Alice")).toBeTruthy();
    mockRoomLive = { userIds: [], available: false };
  });
});
