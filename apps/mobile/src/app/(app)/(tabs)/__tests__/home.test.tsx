import { fireEvent, render, screen, waitFor, within } from "@testing-library/react-native";
import { StyleSheet } from "react-native";
import type { LocalSession, RoomDetail, RoomMember, RoomSummary } from "@lightbulb/shared";

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest — and every reference INSIDE a
// factory is lazy: babel lowers these `const`s to `var`, and the factory runs
// while the screen's own import is still resolving.
const mockPush = jest.fn();
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: (...args: unknown[]) => mockPush(...args) },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(cb, [cb]),
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
  mockSummary({ roomId: "r2", kind: "dm", unreadCount: 3, lastMessageAt: "2026-09-11T11:00:00Z" }),
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

const mockRefreshRooms = jest.fn();
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

import Home, { sectionRooms } from "../home";

describe("Home", () => {
  beforeEach(() => {
    mockPush.mockClear();
    mockRefreshRooms.mockClear();
    mockSetRoomPrefs.mockClear();
    mockSetRoomPrefs.mockImplementation(async () => {});
    mockMarkUnread.mockClear();
    mockSetStringAsync.mockClear();
    mockRoomsState = {};
  });

  it("groups rooms under Channels and Direct messages, newest first in each", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("Channels")).toBeTruthy());
    expect(screen.getByText("Direct messages")).toBeTruthy();
    const channels = screen.getAllByTestId(/^room-row-r[13]$/).map((n) => n.props.testID);
    expect(channels).toEqual(["room-row-r3", "room-row-r1"]);
  });

  it("renders the section headers with the web's exact wording", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("Starred")).toBeTruthy());
    expect(screen.getByText("Channels")).toBeTruthy();
    expect(screen.getByText("Direct messages")).toBeTruthy();
  });

  it("puts a starred room in its own section, out of Channels", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("Starred")).toBeTruthy());
    // #ops is the second-OLDEST room and still renders first: Starred outranks
    // recency, which is the whole point of the section.
    const rows = screen.getAllByTestId(/^room-row-/).map((n) => n.props.testID);
    expect(rows[0]).toBe("room-row-r4");
  });

  it("badges a DM with unreads and names it after the other party", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("Friday")).toBeTruthy());
    expect(screen.getByLabelText("Friday, 3 unread")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
  });

  it("dims a muted room and gives it no badge", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("Direct messages")).toBeTruthy());
    const row = screen.getByTestId("room-row-r5");
    expect(StyleSheet.flatten(row.props.style).opacity).toBe(0.55);
    expect(screen.queryByText("2")).toBeNull();
    expect(screen.getByLabelText("Direct message")).toBeTruthy();
  });

  it("draws a channel's # once, as the glyph column and not as a prefix", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("launch")).toBeTruthy());
    const row = screen.getByTestId("room-row-r1");
    expect(within(row).getAllByText("#")).toHaveLength(1);
    expect(within(row).getByText("launch")).toBeTruthy();
  });

  it("stacks both other parties' avatars on a group DM", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByLabelText("Bob and Carol")).toBeTruthy());
    // A human tile carries initials, an agent tile its first letter — one each.
    expect(screen.getByText("B")).toBeTruthy();
    expect(screen.getByText("C")).toBeTruthy();
  });

  it("opens the room route when a row is pressed", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("random")).toBeTruthy());
    await fireEvent.press(screen.getByText("random"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/r3");
  });

  it("collapses a section and remembers nothing beyond the screen", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("random")).toBeTruthy());
    await fireEvent.press(screen.getByText("Channels"));
    expect(screen.queryByText("random")).toBeNull();
  });

  it("filters the list by the header search field", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("random")).toBeTruthy());
    await fireEvent.changeText(screen.getByPlaceholderText("Search"), "laun");
    expect(screen.getByText("launch")).toBeTruthy();
    expect(screen.queryByText("random")).toBeNull();
    // The # is a glyph, not part of the name — typing it must still match.
    await fireEvent.changeText(screen.getByPlaceholderText("Search"), "#laun");
    expect(screen.getByText("launch")).toBeTruthy();
  });

  it("opens the new-message sheet from the pencil and routes to the room it picks", async () => {
    await render(<Home />);
    expect(screen.queryByText("MOCK_NEW_MESSAGE_SHEET")).toBeNull();
    await fireEvent.press(screen.getByLabelText("New message"));
    await fireEvent.press(await screen.findByText("MOCK_NEW_MESSAGE_SHEET"));
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-9"));
    // The sheet closes behind the push, so coming back does not land on it.
    expect(screen.queryByText("MOCK_NEW_MESSAGE_SHEET")).toBeNull();
  });

  it("offers a retry when the conversation list fails to load", async () => {
    mockRoomsState = { rooms: [], roomsError: new Error("offline") };
    await render(<Home />);
    await fireEvent.press(await screen.findByLabelText("Retry loading conversations"));
    expect(mockRefreshRooms).toHaveBeenCalled();
  });

  it("shows a Muted text tag on a muted room, not just the dimming", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("Direct messages")).toBeTruthy());
    expect(within(screen.getByTestId("room-row-r5")).getByText("Muted")).toBeTruthy();
    // The unstarred, unmuted rows above don't carry the tag.
    expect(within(screen.getByTestId("room-row-r1")).queryByText("Muted")).toBeNull();
  });

  it("opens the conversation actions sheet on a long press, matching the room's own state", async () => {
    await render(<Home />);
    // r4 is starred and unmuted.
    await fireEvent(await screen.findByTestId("room-row-r4"), "longPress");
    expect(screen.getByText("Unstar conversation")).toBeTruthy();
    expect(screen.getByText("Mute conversation")).toBeTruthy();
    expect(screen.getByText("Mark unread")).toBeTruthy();
    expect(screen.getByText("Copy link")).toBeTruthy();
    expect(screen.queryByText("Leave channel")).toBeNull();
  });

  it("stars a room from the sheet, calling setRoomPrefs with the inverted value", async () => {
    await render(<Home />);
    await fireEvent(await screen.findByTestId("room-row-r1"), "longPress");
    await fireEvent.press(screen.getByText("Star conversation"));
    await waitFor(() => expect(mockSetRoomPrefs).toHaveBeenCalledWith("r1", { starred: true }));
  });

  it("shows the web's copy when a mute save fails, and marks unread and copies the link otherwise", async () => {
    mockSetRoomPrefs.mockImplementationOnce(async () => {
      throw new Error("offline");
    });
    await render(<Home />);
    await fireEvent(await screen.findByTestId("room-row-r3"), "longPress");
    await fireEvent.press(screen.getByText("Mute conversation"));
    await waitFor(() =>
      expect(screen.getByText("The notification setting could not be saved.")).toBeTruthy(),
    );

    await fireEvent.press(screen.getByText("Mark unread"));
    expect(mockMarkUnread).toHaveBeenCalledWith("r3");

    await fireEvent(await screen.findByTestId("room-row-r3"), "longPress");
    await fireEvent.press(screen.getByText("Copy link"));
    await waitFor(() =>
      expect(mockSetStringAsync).toHaveBeenCalledWith(expect.stringContaining("room=r3")),
    );
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
    await render(<Home />);
    await waitFor(() => expect(screen.getByLabelText("Dana, sharing a terminal session")).toBeTruthy());
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
