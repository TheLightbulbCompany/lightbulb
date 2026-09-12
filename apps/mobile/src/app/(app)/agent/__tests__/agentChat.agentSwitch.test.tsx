import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react-native";

// expo-router isn't in the jest transform allowlist; stub what AgentChat uses.
// mockParams is mutable so a test can navigate the persisted screen to another
// agent the way expo-router does: same component, new params.
let mockParams: Record<string, string | undefined> = {
  agentId: "agent_a",
  name: "Friday",
};
jest.mock("expo-router", () => {
  const { useEffect } = jest.requireActual("react");
  return {
    router: {
      push: jest.fn(),
      back: jest.fn(),
      canGoBack: () => false,
      replace: jest.fn(),
      setParams: jest.fn(),
    },
    Stack: { Screen: () => null },
    useFocusEffect: (callback: () => void | (() => void)) => useEffect(callback, [callback]),
    useLocalSearchParams: () => mockParams,
  };
});

// `listMessages` resolves empty, so history hydration adds nothing — which is
// exactly the trap this suite pins: nothing but the switch itself may be
// relied on to clear a stale conversation. `userId` is required: the shared
// useRooms keys its SWR scope on it and fetches nothing without one.
jest.mock("@clerk/clerk-expo", () => ({
  useUser: () => ({ user: null }),
  useAuth: () => ({ userId: "user_a", getToken: jest.fn(async () => "token") }),
}));

jest.mock("expo-crypto", () => ({
  randomUUID: () => "00000000-0000-4000-8000-000000000000",
}));

const mockSendMessage = jest.fn(async (_roomId: string, request: { clientMessageId: string; text: string }) => ({
  messageId: request.clientMessageId,
  roomId: `room_${mockParams.agentId}`,
  messageSeq: 1,
  roomRevision: 1,
  author: { kind: "human", id: "user_a", displayName: "You" },
  text: request.text,
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-09T00:00:00.000Z",
  runId: null,
}));
const mockRoom = (agentId: string) => ({
  roomId: `room_${agentId}`,
  name: null,
  kind: "dm",
  roomRevision: 1,
  lastMessageSeq: 0,
  lastMessageAt: null,
  unreadCount: 0,
  updatedAt: "2026-09-09T00:00:00.000Z",
  deliveryKey: `delivery_${agentId}`,
  members: [{
    kind: "agent",
    id: agentId,
    displayName: agentId === "agent_a" ? "Friday" : "Atlas",
    mentionHandle: agentId,
    role: "member",
    listenMode: "active",
    joinedAt: "2026-09-09T00:00:00.000Z",
    joinedSeq: 0,
  }],
  lastReadSeq: 0,
  createdAt: "2026-09-09T00:00:00.000Z",
});
const mockMessaging = {
  listRooms: jest.fn(async () => ({ rooms: [mockRoom("agent_a"), mockRoom("agent_b")] })),
  getRoom: jest.fn(async (roomId: string) => mockRoom(roomId.replace("room_", ""))),
  listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
  markRead: jest.fn(async (_roomId: string, request: { lastReadSeq: number }) => request),
  sendMessage: mockSendMessage,
  abortRun: jest.fn(),
};
const mockChannels = {
  agent_a: ["/rooms/room_agent_a/delivery_agent_a"],
  agent_b: ["/rooms/room_agent_b/delivery_agent_b"],
};
let mockActiveRoomSubscription: { roomId: string; deliveryKey: string } | null = null;
let mockActiveRoomClaim: symbol | null = null;
const mockRoomReleases: Array<() => void> = [];
const mockClaimActiveRoomSubscription = jest.fn((room: { roomId: string; deliveryKey: string }) => {
  const claim = Symbol();
  mockActiveRoomClaim = claim;
  mockActiveRoomSubscription = room;
  const release = () => {
    if (mockActiveRoomClaim !== claim) return;
    mockActiveRoomClaim = null;
    mockActiveRoomSubscription = null;
  };
  mockRoomReleases.push(release);
  return release;
});
const mockOnEvent = () => () => {};
jest.mock("../../../../providers/live-events", () => ({
  useLiveEvents: () => ({
    httpReady: true,
    liveConnected: true,
    liveChannels: mockChannels[mockParams.agentId === "agent_b" ? "agent_b" : "agent_a"],
    liveError: null,
    connectionRevision: 1,
    messaging: mockMessaging,
    runtime: { rpc: jest.fn() },
    claimActiveRoomSubscription: mockClaimActiveRoomSubscription,
    onEvent: mockOnEvent,
  }),
}));

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useRuntimeRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

async function converseAsFriday(): Promise<void> {
  fireEvent.changeText(
    screen.getByPlaceholderText("Ask anything"),
    "friday question",
  );
  await screen.findByDisplayValue("friday question");
  fireEvent.press(screen.getByLabelText("Send"));
  await screen.findByText("friday question");
}

describe("AgentChat agent switching", () => {
  beforeEach(() => {
    mockParams = { agentId: "agent_a", name: "Friday" };
    mockSendMessage.mockClear();
    mockClaimActiveRoomSubscription.mockClear();
    mockActiveRoomSubscription = null;
    mockActiveRoomClaim = null;
    mockRoomReleases.length = 0;
  });

  afterEach(async () => {
    await cleanup();
  });

  it("never shows the previous agent's conversation after switching", async () => {
    const view = await render(<AgentChat />);
    await converseAsFriday();

    // Navigate the persisted screen to Atlas — new params, same component.
    mockParams = { agentId: "agent_b", name: "Atlas" };
    await view.rerender(<AgentChat />);
    await waitFor(() => expect(mockClaimActiveRoomSubscription).toHaveBeenCalledWith({
      roomId: "room_agent_b",
      deliveryKey: "delivery_agent_b",
    }));

    // Friday's transcript must be gone IMMEDIATELY — not "once Atlas's
    // history fetch replaces it" (here that fetch returns nothing). This is
    // the stale-conversation flash: the General→General switch hit
    // switchSession's same-session early return and cleared nothing.
    expect(screen.queryByText("friday question")).toBeNull();
    expect(screen.getByText(/Start a conversation/)).toBeTruthy();
  });

  it("reclaims the focused room after navigation and ignores stale releases", async () => {
    const view = await render(<AgentChat />);
    await waitFor(() => expect(mockClaimActiveRoomSubscription).toHaveBeenCalledWith({
      roomId: "room_agent_a",
      deliveryKey: "delivery_agent_a",
    }));
    const releaseAgentA = mockRoomReleases.at(-1)!;

    mockParams = { agentId: "agent_b", name: "Atlas" };
    await view.rerender(<AgentChat />);
    await waitFor(() => expect(mockActiveRoomSubscription).toEqual({
      roomId: "room_agent_b",
      deliveryKey: "delivery_agent_b",
    }));

    releaseAgentA();
    expect(mockActiveRoomSubscription).toEqual({
      roomId: "room_agent_b",
      deliveryKey: "delivery_agent_b",
    });

    mockParams = { agentId: "agent_a", name: "Friday" };
    await view.rerender(<AgentChat />);
    await waitFor(() => expect(mockActiveRoomSubscription).toEqual({
      roomId: "room_agent_a",
      deliveryKey: "delivery_agent_a",
    }));
  });
});
