import { fireEvent, render, screen, waitFor, within } from "@testing-library/react-native";
import { StyleSheet } from "react-native";
import type { RoomDetail, RoomMember, RoomSummary } from "@lightbulb/shared";

// Every out-of-scope name a jest.mock factory reaches for is `mock`-prefixed —
// babel-plugin-jest-hoist rejects the rest — and every reference INSIDE a
// factory is lazy: babel lowers these `const`s to `var`, and the factory runs
// while the screen's own import is still resolving.
const mockPush = jest.fn();
jest.mock("expo-router", () => ({
  router: { push: (...args: unknown[]) => mockPush(...args) },
}));
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a" }),
  useOrganization: () => ({ organization: null }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "55555555-5555-4555-8555-555555555555" }));
// The roster the pencil picks from reads through the live-events runtime, which
// throws outside its provider.
jest.mock("../../../providers/live-events", () => ({
  useLiveEvents: () => ({
    httpReady: true,
    liveConnected: true,
    liveError: null,
    connectionRevision: 1,
    rpc: jest.fn(),
    getOperation: jest.fn(),
    onEvent: () => () => {},
  }),
}));

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
const mockSelectAgentDm = jest.fn(async (_agentId: string): Promise<string | null> => "room-9");
let mockRoomsState: Record<string, unknown> = {};
jest.mock("../../../lib/rooms", () => ({
  useMobileRooms: () => ({
    rooms: mockRooms,
    directoryRoomDetails: mockDetails,
    roomsLoading: false,
    roomsError: undefined,
    directoryError: undefined,
    refreshRooms: mockRefreshRooms,
    selectAgentDm: mockSelectAgentDm,
    ...mockRoomsState,
  }),
}));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useAgents: () => ({
    agents: [{ id: "agent-1", name: "Friday" }],
    isLoading: false,
    isSettled: true,
    refresh: jest.fn(),
  }),
}));

import Home, { sectionRooms } from "../home";

describe("Home", () => {
  beforeEach(() => {
    mockPush.mockClear();
    mockRefreshRooms.mockClear();
    mockSelectAgentDm.mockClear();
    mockRoomsState = {};
  });

  it("groups rooms under Channels and Direct messages, newest first in each", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("CHANNELS")).toBeTruthy());
    expect(screen.getByText("DIRECT MESSAGES")).toBeTruthy();
    const channels = screen.getAllByTestId(/^room-row-r[13]$/).map((n) => n.props.testID);
    expect(channels).toEqual(["room-row-r3", "room-row-r1"]);
  });

  it("puts a starred room in its own section, out of Channels", async () => {
    await render(<Home />);
    await waitFor(() => expect(screen.getByText("STARRED")).toBeTruthy());
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
    await waitFor(() => expect(screen.getByText("DIRECT MESSAGES")).toBeTruthy());
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
    await fireEvent.press(screen.getByText("CHANNELS"));
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

  it("opens the picked agent's DM room from the pencil", async () => {
    await render(<Home />);
    await fireEvent.press(screen.getByLabelText("New message"));
    await fireEvent.press(await screen.findByLabelText("Friday"));
    await waitFor(() => expect(mockSelectAgentDm).toHaveBeenCalledWith("agent-1"));
    await waitFor(() => expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-9"));
  });

  it("offers a retry when the conversation list fails to load", async () => {
    mockRoomsState = { rooms: [], roomsError: new Error("offline") };
    await render(<Home />);
    await fireEvent.press(await screen.findByLabelText("Retry loading conversations"));
    expect(mockRefreshRooms).toHaveBeenCalled();
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
