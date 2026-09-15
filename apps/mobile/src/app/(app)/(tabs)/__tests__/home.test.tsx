import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react-native";
import * as Haptics from "expo-haptics";
import { useSyncExternalStore, type ReactElement, type ReactNode } from "react";
import { StyleSheet, TextInput, View } from "react-native";
import type { LocalSession, RoomDetail, RoomMember, RoomSummary } from "@lightbulb/shared";

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest — and every reference INSIDE a
// factory is lazy: babel lowers these `const`s to `var`, and the factory runs
// while the screen's own import is still resolving.
const mockPush = jest.fn();
const mockNavigate = jest.fn();
let mockFocused = true;
// The tab bar's selection, as the tab navigator's state reports it; the screen
// under test is always the tab keyed "tab-home".
let mockSelectedTab = "tab-home";

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
    router: { push: (...args: unknown[]) => mockPush(...args) },
    Link,
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(cb, [cb]),
    useIsFocused: () => mockFocused,
    useNavigation: () => mockParentNavigation,
    useRoute: () => ({ key: "tab-home" }),
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
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a" }),
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
const mockRooms: RoomSummary[] = [
  mockSummary({ roomId: "r1", kind: "channel", name: "launch", lastMessageAt: "2026-09-11T09:00:00Z" }),
  mockSummary({ roomId: "r2", kind: "dm", unreadCount: 3, lastMessageSeq: 7, lastMessageAt: "2026-09-11T11:00:00Z" }),
  mockSummary({ roomId: "r3", kind: "channel", name: "random", lastMessageAt: "2026-09-11T12:00:00Z" }),
  mockSummary({ roomId: "r4", kind: "channel", name: "ops", starred: true, lastMessageAt: "2026-09-11T08:00:00Z" }),
  mockSummary({ roomId: "r5", kind: "dm", muted: true, unreadCount: 2, lastMessageAt: "2026-09-11T07:00:00Z" }),
  mockSummary({ roomId: "r6", kind: "group-dm", lastMessageAt: "2026-09-11T06:00:00Z" }),
];
const mockMember = (over: Partial<RoomMember>): RoomMember => ({
  kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null,
  role: "member", listenMode: null, joinedAt: "x", joinedSeq: 0, ...over,
});
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
// Only useWorkspaceSessions' shape matters here -- this suite doesn't cover the
// sharing badge itself, just that the list still renders around it. Declared
// OUTSIDE the mock factory so it's the same object on every render -- a fresh
// literal per call changes `client`'s identity every render, which re-fires
// useWorkspaceSessions' effect forever.
const mockResourceSource = {
  client: { listWorkspaceSessions: jest.fn(async () => ({ sessions: [] as LocalSession[] })) },
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
    ...mockRoomsState,
  }),
}));
jest.mock("../../../../components/room/NewMessageSheet", () => {
  const { Text: RNText } = require("react-native");
  return {
    NewMessageSheet: ({
      visible,
      onOpenRoom,
    }: {
      visible: boolean;
      onOpenRoom: (roomId: string) => void;
    }) =>
      visible ? (
        <RNText onPress={() => onOpenRoom("room-9")}>MOCK_NEW_MESSAGE_SHEET</RNText>
      ) : null,
  };
});

import Dms from "../dms";
import Home, { sectionRooms } from "../home";

/** The tab, under the header the (app) Stack would draw above it. */
const HomeTab = () => (
  <>
    <MockHeader />
    <Home />
  </>
);
const menuOf = (roomId: string) => within(screen.getByTestId(`link-/(app)/room/${roomId}`));

describe("Home", () => {
  beforeEach(() => {
    mockPush.mockClear();
    mockNavigate.mockClear();
    mockRefreshRooms.mockClear();
    mockRefreshRooms.mockImplementation(async () => {});
    mockSetRoomPrefs.mockClear();
    mockSetRoomPrefs.mockImplementation(async () => {});
    mockMarkUnread.mockClear();
    mockMarkRead.mockClear();
    mockSetStringAsync.mockClear();
    mockSwipeClose.mockClear();
    mockSetOptions.mockClear();
    (Haptics.selectionAsync as jest.Mock).mockClear();
    mockHeaderOptions = {};
    mockSelectedTab = "tab-home";
    mockFocused = true;
    mockRoomsState = {};
  });

  it("groups rooms under Channels and Direct messages, newest first in each", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("Channels")).toBeTruthy());
    expect(screen.getByText("Direct messages")).toBeTruthy();
    const channels = screen.getAllByTestId(/^room-row-r[13]$/).map((n) => n.props.testID);
    expect(channels).toEqual(["room-row-r3", "room-row-r1"]);
  });

  it("renders the section headers with the web's exact wording", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("Starred")).toBeTruthy());
    expect(screen.getByText("Channels")).toBeTruthy();
    expect(screen.getByText("Direct messages")).toBeTruthy();
  });

  it("puts a starred room in its own section, out of Channels", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("Starred")).toBeTruthy());
    // #ops is the second-OLDEST room and still renders first: Starred outranks
    // recency, which is the whole point of the section.
    const rows = screen.getAllByTestId(/^room-row-/).map((n) => n.props.testID);
    expect(rows[0]).toBe("room-row-r4");
  });

  it("badges a DM with unreads and names it after the other party", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("Friday")).toBeTruthy());
    expect(screen.getByLabelText("Friday, 3 unread")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
  });

  it("dims a muted room and gives it no badge", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("Direct messages")).toBeTruthy());
    const row = screen.getByTestId("room-row-r5");
    expect(StyleSheet.flatten(row.props.style).opacity).toBe(0.55);
    expect(screen.queryByText("2")).toBeNull();
    expect(screen.getByLabelText("Direct message")).toBeTruthy();
  });

  it("draws a channel's # once, as the glyph column and not as a prefix", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("launch")).toBeTruthy());
    const row = screen.getByTestId("room-row-r1");
    expect(within(row).getAllByText("#")).toHaveLength(1);
    expect(within(row).getByText("launch")).toBeTruthy();
  });

  it("stacks both other parties' avatars on a group DM", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByLabelText("Bob and Carol")).toBeTruthy());
    // A human tile carries initials, an agent tile its first letter — one each.
    expect(screen.getByText("B")).toBeTruthy();
    expect(screen.getByText("C")).toBeTruthy();
  });

  it("opens the room route when a row is pressed", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("random")).toBeTruthy());
    await fireEvent.press(screen.getByText("random"));
    expect(mockNavigate).toHaveBeenCalledWith("/(app)/room/r3");
  });

  it("collapses a section and remembers nothing beyond the screen", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("random")).toBeTruthy());
    await fireEvent.press(screen.getByText("Channels"));
    expect(screen.queryByText("random")).toBeNull();
  });

  it("filters the list by the header's native search bar", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("random")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Search"), "laun");
    expect(screen.getByText("launch")).toBeTruthy();
    expect(screen.queryByText("random")).toBeNull();
    // The # is a glyph, not part of the name — typing it must still match.
    await fireEvent.changeText(screen.getByPlaceholderText("Search"), "#laun");
    expect(screen.getByText("launch")).toBeTruthy();
    // Case-insensitive, and an empty field shows everything again.
    await fireEvent.changeText(screen.getByPlaceholderText("Search"), "LAUNCH");
    expect(screen.getByText("launch")).toBeTruthy();
    await fireEvent.changeText(screen.getByPlaceholderText("Search"), "");
    expect(screen.getByText("random")).toBeTruthy();
  });

  it("empties the search and hands the header back when another tab is selected", async () => {
    const view = await render(<HomeTab />);
    await fireEvent.changeText(await screen.findByPlaceholderText("Search"), "laun");
    expect(screen.queryByText("random")).toBeNull();

    mockSelectedTab = "tab-activity";
    await view.rerender(<HomeTab />);
    expect(mockSetOptions).toHaveBeenLastCalledWith({
      headerSearchBarOptions: undefined,
      headerRight: undefined,
    });
    expect(screen.queryByPlaceholderText("Search")).toBeNull();
    expect(screen.queryByLabelText("New message")).toBeNull();

    mockSelectedTab = "tab-home";
    await view.rerender(<HomeTab />);
    expect(screen.getByPlaceholderText("Search")).toBeTruthy();
    expect(screen.getByText("random")).toBeTruthy();
  });

  // A room pushed over the tabs blurs the tab; the header is the outgoing
  // screen's and must ride the push out whole, search and all.
  it("keeps the header and the search while a room is pushed over the tabs", async () => {
    const view = await render(<HomeTab />);
    await fireEvent.changeText(await screen.findByPlaceholderText("Search"), "laun");
    mockSetOptions.mockClear();

    mockFocused = false;
    await view.rerender(<HomeTab />);
    expect(mockSetOptions).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("Search")).toBeTruthy();
    expect(screen.queryByText("random")).toBeNull();
  });

  it("opens the new-message sheet from the header's compose button and routes to the room it picks", async () => {
    await render(<HomeTab />);
    expect(screen.queryByText("MOCK_NEW_MESSAGE_SHEET")).toBeNull();
    await fireEvent.press(await screen.findByLabelText("New message"));
    await fireEvent.press(await screen.findByText("MOCK_NEW_MESSAGE_SHEET"));
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-9"));
    // The sheet closes behind the push, so coming back does not land on it.
    expect(screen.queryByText("MOCK_NEW_MESSAGE_SHEET")).toBeNull();
  });

  it("refreshes the conversation list on pull, spinning until it lands", async () => {
    let land = () => {};
    mockRefreshRooms.mockImplementation(() => new Promise<void>((resolve) => { land = resolve; }));
    await render(<HomeTab />);
    const list = await screen.findByTestId("room-list");
    expect(list.props.refreshing).toBe(false);
    await act(async () => { list.props.onRefresh(); });
    expect(mockRefreshRooms).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("room-list").props.refreshing).toBe(true);
    await act(async () => land());
    expect(screen.getByTestId("room-list").props.refreshing).toBe(false);
  });

  it("offers a retry when the conversation list fails to load", async () => {
    mockRoomsState = { rooms: [], roomsError: new Error("offline") };
    await render(<HomeTab />);
    await fireEvent.press(await screen.findByLabelText("Retry loading conversations"));
    expect(mockRefreshRooms).toHaveBeenCalled();
  });

  it("shows a Muted text tag on a muted room, not just the dimming", async () => {
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByText("Direct messages")).toBeTruthy());
    expect(within(screen.getByTestId("room-row-r5")).getByText("Muted")).toBeTruthy();
    // The unstarred, unmuted rows above don't carry the tag.
    expect(within(screen.getByTestId("room-row-r1")).queryByText("Muted")).toBeNull();
  });

  describe("context menu", () => {
    it("offers the four conversation actions, worded for the room's own state", async () => {
      await render(<HomeTab />);
      // r4 is starred and unmuted.
      await waitFor(() => expect(screen.getByText("ops")).toBeTruthy());
      expect(menuOf("r4").getByText("Unstar conversation")).toBeTruthy();
      expect(menuOf("r4").getByText("Mute conversation")).toBeTruthy();
      expect(menuOf("r4").getByText("Mark unread")).toBeTruthy();
      expect(menuOf("r4").getByText("Copy link")).toBeTruthy();
      expect(menuOf("r4").queryByText("Leave channel")).toBeNull();
      // r5 is muted.
      expect(menuOf("r5").getByText("Unmute conversation")).toBeTruthy();
    });

    it("stars a room, calling setRoomPrefs with the inverted value", async () => {
      await render(<HomeTab />);
      await waitFor(() => expect(screen.getByText("launch")).toBeTruthy());
      await fireEvent.press(menuOf("r1").getByText("Star conversation"));
      expect(mockSetRoomPrefs).toHaveBeenCalledWith("r1", { starred: true });
    });

    it("shows the web's copy above the list when a mute save fails, and clears it on the next try", async () => {
      mockSetRoomPrefs.mockImplementationOnce(async () => {
        throw new Error("offline");
      });
      await render(<HomeTab />);
      await waitFor(() => expect(screen.getByText("random")).toBeTruthy());
      await fireEvent.press(menuOf("r3").getByText("Mute conversation"));
      expect(mockSetRoomPrefs).toHaveBeenCalledWith("r3", { muted: true });
      expect(await screen.findByText("The notification setting could not be saved.")).toBeTruthy();

      await fireEvent.press(menuOf("r3").getByText("Star conversation"));
      await waitFor(() =>
        expect(screen.queryByText("The notification setting could not be saved.")).toBeNull(),
      );
    });

    it("marks a room unread and copies its link", async () => {
      await render(<HomeTab />);
      await waitFor(() => expect(screen.getByText("random")).toBeTruthy());
      await fireEvent.press(menuOf("r3").getByText("Mark unread"));
      expect(mockMarkUnread).toHaveBeenCalledWith("r3");
      await fireEvent.press(menuOf("r3").getByText("Copy link"));
      expect(mockSetStringAsync).toHaveBeenCalledWith(expect.stringContaining("room=r3"));
    });
  });

  describe("swipe actions", () => {
    // One unread DM and one read channel, so each action label is unique.
    beforeEach(() => {
      mockRoomsState = { rooms: [mockRooms[1], mockRooms[0]] };
    });
    // Out of VoiceOver's tree on purpose (the row carries the same actions),
    // so the queries have to ask for hidden elements.
    const hidden = { includeHiddenElements: true };

    it("marks an unread room read up to its newest message, then closes with a haptic", async () => {
      await render(<HomeTab />);
      await fireEvent.press(await screen.findByLabelText("Mark read", hidden));
      expect(mockSwipeClose).toHaveBeenCalledTimes(1);
      expect(Haptics.selectionAsync).toHaveBeenCalledTimes(1);
      expect(mockMarkRead).toHaveBeenCalledWith("r2", { lastReadSeq: 7 });
      // The list re-reads for the new count once the cursor has moved.
      await waitFor(() => expect(mockRefreshRooms).toHaveBeenCalledTimes(1));
    });

    it("offers Mark unread on a room that is already read", async () => {
      await render(<HomeTab />);
      await fireEvent.press(await screen.findByLabelText("Mark unread", hidden));
      expect(mockMarkUnread).toHaveBeenCalledWith("r1");
      expect(mockMarkRead).not.toHaveBeenCalled();
    });

    it("mutes and unmutes from the trailing swipe", async () => {
      mockRoomsState = { rooms: [mockRooms[0], mockRooms[4]] };
      await render(<HomeTab />);
      await fireEvent.press(await screen.findByLabelText("Mute conversation", hidden));
      expect(mockSetRoomPrefs).toHaveBeenCalledWith("r1", { muted: true });
      await fireEvent.press(screen.getByLabelText("Unmute conversation", hidden));
      expect(mockSetRoomPrefs).toHaveBeenCalledWith("r5", { muted: false });
      expect(mockSwipeClose).toHaveBeenCalledTimes(2);
    });

    it("hands VoiceOver the same actions on the row itself", async () => {
      await render(<HomeTab />);
      const row = await screen.findByTestId("room-row-r2");
      expect(row.props.accessibilityActions.map((action: { label: string }) => action.label)).toEqual([
        "Mark read",
        "Mute conversation",
        "Star conversation",
        "Copy link",
      ]);
      await fireEvent(row, "accessibilityAction", { nativeEvent: { actionName: "star" } });
      expect(mockSetRoomPrefs).toHaveBeenCalledWith("r2", { starred: true });
    });
  });

  it("badges a DM row whose counterpart owns a live shared session", async () => {
    const sharingRoom = mockSummary({ roomId: "r7", kind: "dm", lastMessageAt: "2026-09-11T12:30:00Z" });
    const sharingDetail: RoomDetail = {
      ...sharingRoom,
      deliveryKey: "k",
      lastReadSeq: 0,
      createdAt: "2026-09-11T09:00:00Z",
      pinnedMessageIds: [],
      members: [mockMember({ role: "owner" }), mockMember({ id: "user-c", displayName: "Dana" })],
    };
    mockRoomsState = { rooms: [sharingRoom], directoryRoomDetails: new Map([["r7", sharingDetail]]) };
    const sharingSession: LocalSession = {
      sessionId: "s1", deviceId: "mac-1", humanUserId: "user-c", provider: "claude", generation: "g1",
      status: "idle", providerVersion: "2.1.224", controlUnavailableReason: null, activeTurnId: null,
      capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
      latestEventSeq: 1,
    };
    mockResourceSource.client.listWorkspaceSessions.mockResolvedValueOnce({ sessions: [sharingSession] });
    await render(<HomeTab />);
    await waitFor(() => expect(screen.getByLabelText("Dana, sharing a terminal session")).toBeTruthy());
  });
});

describe("DMs", () => {
  beforeEach(() => {
    mockHeaderOptions = {};
    mockSelectedTab = "tab-home";
    mockRoomsState = {};
  });

  it("lists only direct conversations, unsectioned and newest first, under the same header", async () => {
    await render(
      <>
        <MockHeader />
        <Dms />
      </>,
    );
    await waitFor(() => expect(screen.getByText("Friday")).toBeTruthy());
    const rows = screen.getAllByTestId(/^room-row-/).map((n) => n.props.testID);
    expect(rows).toEqual(["room-row-r2", "room-row-r5", "room-row-r6"]);
    expect(screen.queryByText("Direct messages")).toBeNull();
    expect(screen.getByPlaceholderText("Search")).toBeTruthy();
  });
});

describe("sectionRooms", () => {
  it("buckets starred out of both lists and orders each by recency", () => {
    const sections = sectionRooms(mockRooms);
    expect(sections.starred.map((room) => room.roomId)).toEqual(["r4"]);
    expect(sections.channels.map((room) => room.roomId)).toEqual(["r3", "r1"]);
    expect(sections.direct.map((room) => room.roomId)).toEqual(["r2", "r5", "r6"]);
  });

  it("falls back to updatedAt for a room that has never had a message", () => {
    const never = mockSummary({ roomId: "r9", kind: "channel", name: "new", lastMessageAt: null, updatedAt: "2026-09-11T23:00:00Z" });
    expect(sectionRooms([...mockRooms, never]).channels[0].roomId).toBe("r9");
  });
});
