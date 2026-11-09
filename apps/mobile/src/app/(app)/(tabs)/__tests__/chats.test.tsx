import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react-native";
import { useSyncExternalStore, type ReactElement, type ReactNode } from "react";
import { AppState } from "react-native";
import { TextInput, View } from "react-native";
import type { LocalSession, RoomDetail, RoomMember, RoomSummary } from "@lightbulb/shared";

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest — and every reference INSIDE a
// factory is lazy: babel lowers these `const`s to `var`, and the factory runs
// while the screen's own import is still resolving.
const mockPush = jest.fn();
const mockSetParams = jest.fn((next: { filter?: string }) => { mockRouteFilter = next.filter; });
let mockRouteFilter: string | undefined;
const mockNavigate = jest.fn();
let mockFocused = true;
// The tab bar's selection, as the tab navigator's state reports it; the screen
// under test is always the tab keyed "tab-chats".
let mockSelectedTab = "tab-chats";
// The pins store is module state shared by every test in the file, so each
// test gets its own workspace (a fresh org id) rather than fighting the last
// test's writes.
let mockOrgId = "org-0";
let mockOrgSeq = 0;

// The (app) Stack's header, as far as this suite needs it: whatever the screen
// hands the parent's `setOptions` is drawn — the search bar as a TextInput that
// reports text the way the native one does, and `headerRight` as-is.
type MockHeaderOptions = {
  headerSearchBarOptions?: {
    placeholder?: string;
    onChangeText?: (event: { nativeEvent: { text: string } }) => void;
  };
  headerRight?: () => ReactNode;
};
let mockHeaderOptions: MockHeaderOptions = {};
const mockHeaderListeners = new Set<() => void>();
const mockSetOptions = jest.fn((options: MockHeaderOptions) => {
  mockHeaderOptions = { ...mockHeaderOptions, ...options };
  for (const listener of mockHeaderListeners) listener();
});
// One object, as react-navigation caches it per route: the screen keys its
// header effect on it, and a fresh one per render would re-run that effect.
const mockParentNavigation = { setOptions: (options: MockHeaderOptions) => mockSetOptions(options) };
function MockHeader() {
  const options = useSyncExternalStore(
    (listener) => {
      mockHeaderListeners.add(listener);
      return () => {
        mockHeaderListeners.delete(listener);
      };
    },
    () => mockHeaderOptions,
  );
  const search = options.headerSearchBarOptions;
  return (
    <View>
      {search && (
        <TextInput
          placeholder={search.placeholder}
          onChangeText={(text) => search.onChangeText?.({ nativeEvent: { text } })}
        />
      )}
      {options.headerRight?.()}
    </View>
  );
}

// expo-router's Link, reduced to its contract here: the trigger's press
// navigates to `href`, the preview is native-only, and the menu's actions are
// drawn as plain buttons (scoped per row by the Link's testID) so a test can
// press them.
jest.mock("expo-router", () => {
  const { cloneElement, createContext, use, useEffect } = require("react");
  const { Pressable, Text, View: RNView } = require("react-native");
  const Href = createContext("");
  function Link({ href, children }: { href: string; children?: ReactNode }) {
    return (
      <Href value={href}>
        <RNView testID={`link-${href}`}>{children}</RNView>
      </Href>
    );
  }
  Link.Trigger = function Trigger({
    children,
  }: {
    children: ReactElement<{ onPress?: () => void }>;
  }) {
    const href = use(Href);
    return cloneElement(children, { onPress: () => mockNavigate(href) });
  };
  Link.Preview = function Preview() {
    return null;
  };
  Link.Menu = function Menu({ children }: { children?: ReactNode }) {
    return <RNView>{children}</RNView>;
  };
  Link.MenuAction = function MenuAction({
    children,
    onPress,
  }: {
    children?: ReactNode;
    onPress?: () => void;
  }) {
    return (
      <Pressable onPress={onPress}>
        <Text>{children}</Text>
      </Pressable>
    );
  };
  return {
    router: { push: (...args: unknown[]) => mockPush(...args), setParams: (...args: [{ filter?: string }]) => mockSetParams(...args) },
    Link,
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(cb, [cb]),
    useIsFocused: () => mockFocused,
    useLocalSearchParams: () => ({ filter: mockRouteFilter }),
    useNavigation: () => mockParentNavigation,
    useRoute: () => ({ key: "tab-chats" }),
  };
});
jest.mock("expo-router/react-navigation", () => ({
  useNavigationState: <T,>(selector: (state: { index: number; routes: { key: string }[] }) => T) =>
    selector({ index: 0, routes: [{ key: mockSelectedTab }] }),
}));
// The swipe container, flattened: both action panels drawn beside the row, the
// way they sit behind it on device, with the `close` a pressed action calls.
const mockSwipeClose = jest.fn();
jest.mock("react-native-gesture-handler/ReanimatedSwipeable", () => {
  const { View: RNView } = require("react-native");
  const swipe = { close: () => mockSwipeClose(), openLeft() {}, openRight() {}, reset() {} };
  const progress = { value: 0 };
  return {
    __esModule: true,
    default: ({
      children,
      renderLeftActions,
      renderRightActions,
    }: {
      children?: ReactNode;
      renderLeftActions?: (p: unknown, t: unknown, s: unknown) => ReactNode;
      renderRightActions?: (p: unknown, t: unknown, s: unknown) => ReactNode;
    }) => (
      <RNView>
        {renderLeftActions?.(progress, progress, swipe)}
        {children}
        {renderRightActions?.(progress, progress, swipe)}
      </RNView>
    ),
  };
});
jest.mock("../../../../components/billing/TrialRecapCard", () => ({ TrialRecapCard: () => null }));
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ userId: "user-a", orgId: mockOrgId }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "55555555-5555-4555-8555-555555555555" }));
const mockSetStringAsync = jest.fn(async (_text: string) => true);
jest.mock("expo-clipboard", () => ({ setStringAsync: (text: string) => mockSetStringAsync(text) }), {
  virtual: true,
});
// Mark read goes to the messaging client directly — the room machine only
// marks the room it has open. One hoisted object, never a fresh literal.
const mockMarkRead = jest.fn(async (_roomId: string, _cursor: { lastReadSeq: number }) => ({}));
const mockLive = { messaging: { markRead: mockMarkRead } };
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

const mockSummary = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "r1",
  name: null,
  kind: "dm",
  roomRevision: 1,
  lastMessageSeq: 1,
  lastMessageAt: "2026-09-11T10:00:00Z",
  unreadCount: 0,
  updatedAt: "2026-09-11T10:00:00Z",
  topic: null,
  starred: false,
  muted: false,
  sectionId: null,
  members: [],
  lastReadSeq: 0,
  ...over,
});
const mockMember = (over: Partial<RoomMember>): RoomMember => ({
  kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null,
  role: "member", listenMode: null, joinedAt: "x", joinedSeq: 0, ...over,
});
const mockRooms: RoomSummary[] = [
  mockSummary({ roomId: "r1", kind: "channel", name: "launch", lastMessageAt: "2026-09-11T09:00:00Z" }),
  mockSummary({
    roomId: "r2", kind: "dm", unreadCount: 3, lastMessageSeq: 7, lastMessageAt: "2026-09-11T11:00:00Z",
    lastMessage: {
      messageId: "m1", seq: 7, authorId: "agent-1", authorKind: "agent", authorName: "Friday",
      textPreview: "On it.", hasAttachments: false, createdAt: "2026-09-11T11:00:00Z", threadRootMessageId: null,
    },
    // Chats reads `room.members` directly (not the directory), so the DM's
    // agent counterpart has to live on the summary itself.
    members: [mockMember({ role: "owner" }), mockMember({ kind: "agent", id: "agent-1", displayName: "Friday" })],
  }),
  mockSummary({ roomId: "r3", kind: "channel", name: "random", lastMessageAt: "2026-09-11T12:00:00Z" }),
  mockSummary({ roomId: "r4", kind: "channel", name: "ops", starred: true, lastMessageAt: "2026-09-11T08:00:00Z" }),
  mockSummary({ roomId: "r5", kind: "dm", muted: true, unreadCount: 2, lastMessageAt: "2026-09-11T07:00:00Z" }),
  mockSummary({ roomId: "r6", kind: "group-dm", lastMessageAt: "2026-09-11T06:00:00Z" }),
];
const mockDetails = new Map<string, RoomDetail>([
  [
    "r2",
    {
      ...mockSummary({ roomId: "r2", kind: "dm", unreadCount: 3 }),
      deliveryKey: "k",
      lastReadSeq: 0,
      createdAt: "2026-09-11T09:00:00Z",
      pinnedMessageIds: [],
      members: [
        mockMember({ role: "owner" }),
        mockMember({ kind: "agent", id: "agent-1", displayName: "Friday", mentionHandle: "friday", listenMode: "active" }),
      ],
    },
  ],
  [
    "r6",
    {
      ...mockSummary({ roomId: "r6", kind: "group-dm" }),
      deliveryKey: "k",
      lastReadSeq: 0,
      createdAt: "2026-09-11T06:00:00Z",
      pinnedMessageIds: [],
      members: [
        mockMember({ role: "owner" }),
        mockMember({ id: "user-b", displayName: "Bob" }),
        mockMember({ kind: "agent", id: "agent-2", displayName: "Carol" }),
      ],
    },
  ],
]);

const mockRefreshRooms = jest.fn(async () => {});
const mockSetRoomPrefs = jest.fn(async (_roomId: string, _prefs: unknown) => {});
const mockMarkUnread = jest.fn(async (_roomId: string) => {});
let mockRoomsState: Record<string, unknown> = {};
// This suite drives the merged list through live sessions, so unlike Home's
// suite the sessions the client returns are test-controlled state. Declared
// OUTSIDE the mock factory so it's the same object on every render -- a fresh
// literal per call changes `client`'s identity every render, which re-fires
// useWorkspaceSessions' effect forever.
let mockSessions: LocalSession[] = [];
const mockResourceSource = {
  client: { listWorkspaceSessions: jest.fn(async () => ({ sessions: mockSessions })) },
  onEvent: () => () => {},
  connectionRevision: 0,
};
jest.mock("../../../../lib/rooms", () => ({
  useMobileRooms: () => ({
    rooms: mockRooms,
    directoryRoomDetails: mockDetails,
    roomsLoading: false,
    roomsError: undefined,
    directoryError: undefined,
    refreshRooms: mockRefreshRooms,
    setRoomPrefs: mockSetRoomPrefs,
    markUnread: mockMarkUnread,
    resourceSource: mockResourceSource,
    selectDm: jest.fn(async () => null),
    ...mockRoomsState,
  }),
}));
let mockAgents: { id: string; name: string; status?: string; seeded?: boolean; origin?: string; role?: string }[] = [];
jest.mock("../../../../lib/workspaceRoster", () => ({
  useWorkspaceRoster: () => ({ agents: mockAgents, members: [{ userId: "user-b", name: "Leo" }] }),
}));
let mockLiveLines: Record<string, string> = {};
jest.mock("../../../../lib/roomActivity", () => ({
  useRoomActivity: () => ({ liveLine: (roomId: string) => mockLiveLines[roomId] ?? null, working: () => false }),
}));
jest.mock("../../../../lib/presence", () => ({ usePresence: () => new Set<string>() }));
jest.mock("../../../../components/room/NewMessageSheet", () => {
  const { Text: RNText, View: RNView } = require("react-native");
  return {
    NewMessageSheet: ({
      visible,
      onOpenRoom,
      onNewSession,
    }: {
      visible: boolean;
      onOpenRoom: (roomId: string) => void;
      onNewSession?: () => void;
    }) =>
      visible ? (
        <RNView>
          <RNText onPress={() => onOpenRoom("room-9")}>MOCK_NEW_MESSAGE_SHEET</RNText>
          {onNewSession ? <RNText onPress={onNewSession}>MOCK_NEW_SESSION</RNText> : null}
        </RNView>
      ) : null,
  };
});


import Chats from "../chats";

const ChatsTab = () => (
  <>
    <MockHeader />
    <Chats />
  </>
);
const busySession: LocalSession = {
  sessionId: "sess-1", deviceId: "mac", humanUserId: "user-b", provider: "claude", generation: "g",
  status: "busy",
  providerVersion: "2", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 4, title: "lightbulb", lastEventAt: "2026-09-11T11:30:00Z",
};

describe("Chats", () => {
  beforeEach(() => {
    mockPush.mockClear();
    mockSetParams.mockClear();
    mockRouteFilter = undefined;
    mockNavigate.mockClear();
    mockRefreshRooms.mockClear();
    mockMarkRead.mockClear();
    mockSetRoomPrefs.mockClear();
    mockHeaderOptions = {};
    mockSelectedTab = "tab-chats";
    mockOrgId = `org-${++mockOrgSeq}`;
    mockFocused = true;
    mockRoomsState = {};
    mockSessions = [];
    mockLiveLines = {};
    mockAgents = [
      { id: "agent-1", name: "Friday", status: "running", seeded: true },
      { id: "agent-2", name: "Carol", status: "idle", seeded: false, origin: "Starter" },
      { id: "agent-9", name: "Hired", status: "idle", seeded: false },
    ];
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  // The grouping itself is lib/chats `fileSpecialists` (unit-tested there); this checks the screen wires it.
  it("the Specialists row opens to the specialist DM", async () => {
    mockAgents = [{ id: "agent-lb", name: "Lightbulb", role: "ceo", origin: "Starter" }, ...mockAgents];
    await render(<ChatsTab />);
    await fireEvent.press(await screen.findByLabelText("Specialists · 1"));
    expect(await screen.findByTestId("chat-room-r2")).toBeTruthy();
  }, 15_000);

  it("merges rooms and live sessions into one list, newest first", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    await waitFor(() => expect(screen.getAllByLabelText(/^Claude · lightbulb/).length).toBeGreaterThan(0));
    const order = screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID);
    expect(order.slice(0, 3)).toEqual(["chat-room-r3", "chat-session-sess-1", "chat-room-r2"]);
  });

  // Owner ruling 2026-09-28: no pinned strip. A running session is a row in
  // the list like any other, and rows offer no Pin.
  it("draws no pinned strip, with agents on the roster and a session running", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    expect(await screen.findByTestId("chat-session-sess-1")).toBeTruthy();
    expect(screen.queryByTestId("pinned-strip")).toBeNull();
    expect(within(screen.getByTestId("link-/(app)/room/r1")).queryByText("Pin")).toBeNull();
  });

  it("filters rows while searching", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    await fireEvent.changeText(await screen.findByPlaceholderText("Search"), "#laun");
    await waitFor(() => expect(screen.queryByTestId("chat-session-sess-1")).toBeNull());
    expect(screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID)).toEqual(["chat-room-r1"]);
  });

  it("opens a session row on its own screen", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    await fireEvent.press(await screen.findByLabelText("Claude · lightbulb, Working, Leo"));
    expect(mockNavigate).toHaveBeenCalledWith("/(app)/session/sess-1");
  });

  it("archives a session off the list", async () => {
    mockSessions = [{ ...busySession, status: "idle" }];
    await render(<ChatsTab />);
    await fireEvent.press(await screen.findByLabelText("Archive session", { includeHiddenElements: true }));
    await waitFor(() => expect(screen.queryByTestId("chat-session-sess-1")).toBeNull());
  });

  it("marks a room read from its swipe", async () => {
    await render(<ChatsTab />);
    await fireEvent.press(within(await screen.findByTestId("chat-room-r2")).getByLabelText("Mark read", { includeHiddenElements: true }));
    await waitFor(() => expect(mockMarkRead).toHaveBeenCalledWith("r2", { lastReadSeq: 7 }));
  });

  it("shows a room's live line from the user-channel hints", async () => {
    mockLiveLines = { r1: "Bob is typing…" };
    await render(<ChatsTab />);
    expect(within(await screen.findByTestId("chat-room-r1")).getByText("Bob is typing…")).toBeTruthy();
  });

  it("pull-to-refresh also refreshes sessions, and a sessions failure shows inline with a retry", async () => {
    mockResourceSource.client.listWorkspaceSessions.mockClear();
    mockResourceSource.client.listWorkspaceSessions.mockRejectedValueOnce(new Error("nope"));
    await render(<ChatsTab />);
    expect(await screen.findByText("Could not refresh shared sessions.")).toBeTruthy();

    mockResourceSource.client.listWorkspaceSessions.mockClear();
    mockRefreshRooms.mockClear();
    await fireEvent(await screen.findByTestId("chat-list"), "refresh");
    // Pull-to-refresh refreshes both halves of the merged list, not just rooms.
    await waitFor(() => expect(mockRefreshRooms).toHaveBeenCalled());
    await waitFor(() => expect(mockResourceSource.client.listWorkspaceSessions).toHaveBeenCalled());
    // The retried call succeeded, so the inline failure clears.
    await waitFor(() => expect(screen.queryByText("Could not refresh shared sessions.")).toBeNull());
  });

  it("refetches sessions when the app returns to the foreground", async () => {
    let onChange: ((state: string) => void) | undefined;
    jest.spyOn(AppState, "addEventListener").mockImplementation((_type, handler) => {
      onChange = handler as (state: string) => void;
      return { remove: () => {} };
    });
    await render(<ChatsTab />);
    await waitFor(() => expect(onChange).toBeDefined());
    mockResourceSource.client.listWorkspaceSessions.mockClear();
    await act(async () => onChange?.("active"));
    await waitFor(() => expect(mockResourceSource.client.listWorkspaceSessions).toHaveBeenCalled());
  });

  it("keeps an ended session in the list", async () => {
    mockSessions = [{ ...busySession, status: "ended", endedAt: "2026-09-11T11:40:00Z" }];
    await render(<ChatsTab />);
    expect(await screen.findByTestId("chat-session-sess-1")).toBeTruthy();
  });

  it("puts the compose button and search in the shared header, with no large title", async () => {
    await render(<ChatsTab />);
    await waitFor(() => expect(mockSetOptions).toHaveBeenCalledWith(expect.objectContaining({ headerSearchBarOptions: expect.anything() })));
    expect(mockSetOptions).not.toHaveBeenCalledWith(expect.objectContaining({ headerLargeTitle: true }));
    await fireEvent.press(screen.getByLabelText("New message"));
    expect(screen.getByText("MOCK_NEW_MESSAGE_SHEET")).toBeTruthy();
  });

  it("filters sessions, channels and direct messages without changing their recency order", async () => {
    mockSessions = [busySession];
    await render(<ChatsTab />);
    await screen.findByTestId("chat-session-sess-1");
    await fireEvent.press(screen.getByLabelText("Sessions"));
    expect(screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID)).toEqual(["chat-session-sess-1"]);
    await fireEvent.press(screen.getByLabelText("Channels"));
    expect(screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID)).toEqual(["chat-room-r3", "chat-room-r1", "chat-room-r4"]);
    await fireEvent.press(screen.getByLabelText("Direct"));
    expect(screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID)).toEqual(["chat-room-r2", "chat-room-r5", "chat-room-r6"]);
  });

  it("consumes each Sessions navigation so the same link works again after choosing All", async () => {
    mockSessions = [busySession];
    mockRouteFilter = "sessions";
    const view = await render(<ChatsTab />);
    await waitFor(() => expect(mockSetParams).toHaveBeenCalledWith({ filter: undefined }));
    expect(screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID)).toEqual(["chat-session-sess-1"]);
    await fireEvent.press(screen.getByLabelText("All"));
    mockRouteFilter = "sessions";
    view.rerender(<ChatsTab />);
    await waitFor(() => expect(screen.getAllByTestId(/^chat-(room|session)-/).map((n) => n.props.testID)).toEqual(["chat-session-sess-1"]));
  });
});

// The Home voice controls have their own interaction tests.
jest.mock("../../../../components/voice/VoiceHome", () => ({ VoiceHome: () => null }));
