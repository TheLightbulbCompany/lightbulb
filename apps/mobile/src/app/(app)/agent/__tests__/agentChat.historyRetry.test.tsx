import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react-native";

jest.mock("expo-router", () => ({
  router: {
    push: jest.fn(),
    back: jest.fn(),
    canGoBack: () => false,
    replace: jest.fn(),
    setParams: jest.fn(),
  },
  Stack: { Screen: () => null },
  useFocusEffect: jest.fn(),
  useLocalSearchParams: () => ({ agentId: "agent_a", name: "Friday" }),
}));

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user_1", getToken: jest.fn(async () => "token") }),
}));

jest.mock("expo-crypto", () => ({
  randomUUID: () => "00000000-0000-4000-8000-000000000000",
}));

const mockRoom = {
  roomId: "room_a",
  name: null,
  kind: "dm",
  roomRevision: 1,
  lastMessageSeq: 1,
  lastMessageAt: "2026-09-09T00:00:00.000Z",
  unreadCount: 0,
  updatedAt: "2026-09-09T00:00:00.000Z",
  deliveryKey: "delivery_a",
  members: [{
    kind: "agent",
    id: "agent_a",
    displayName: "Friday",
    mentionHandle: "friday",
    role: "member",
    listenMode: "active",
    joinedAt: "2026-09-09T00:00:00.000Z",
    joinedSeq: 0,
  }],
  lastReadSeq: 0,
  createdAt: "2026-09-09T00:00:00.000Z",
};
const mockListMessages = jest.fn();
const mockMessaging = {
  listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
  getRoom: jest.fn(async () => mockRoom),
  listMessages: mockListMessages,
  markRead: jest.fn(async () => ({ roomId: "room_a", lastReadSeq: 1 })),
  sendMessage: jest.fn(),
  abortRun: jest.fn(),
};
const mockLiveChannels = ["/rooms/room_a/delivery_a"];
const mockClaimActiveRoomSubscription = jest.fn(() => () => {});
const mockOnEvent = () => () => {};
jest.mock("../../../../providers/live-events", () => ({
  useLiveEvents: () => ({
    httpReady: true,
    liveConnected: true,
    liveChannels: mockLiveChannels,
    liveError: null,
    connectionRevision: 1,
    messaging: mockMessaging,
    claimActiveRoomSubscription: mockClaimActiveRoomSubscription,
    onEvent: mockOnEvent,
  }),
}));

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useRuntimeRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

const canonicalMessage = {
  messageId: "message_1",
  roomId: "room_a",
  messageSeq: 1,
  roomRevision: 1,
  author: { kind: "agent", id: "agent_a", displayName: "Friday" },
  text: "the transcript survived",
  mentions: [],
  replyToMessageId: null,
  threadRootMessageId: null,
  createdAt: "2026-09-09T00:00:00.000Z",
  runId: "run_1",
};

describe("AgentChat canonical history retry", () => {
  beforeEach(() => {
    mockListMessages.mockReset();
    mockClaimActiveRoomSubscription.mockClear();
  });

  afterEach(async () => {
    await cleanup();
  });

  it("lets the user retry a failed canonical history read", async () => {
    mockListMessages
      .mockRejectedValueOnce(new Error("History is temporarily unavailable"))
      .mockResolvedValue({ messages: [canonicalMessage], nextAfterSeq: null });

    await render(<AgentChat />);
    expect(await screen.findByText(/History is temporarily unavailable/)).toBeTruthy();

    fireEvent.press(screen.getByLabelText("Retry loading history"));

    await waitFor(() => expect(mockListMessages.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(await screen.findByText("the transcript survived")).toBeTruthy();
    expect(screen.queryByText(/Couldn’t load history/)).toBeNull();
  });
});
