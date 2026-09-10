import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react-native";

jest.mock("expo-router", () => {
  const { useEffect } = jest.requireActual("react");
  return {
    router: { push: jest.fn(), back: jest.fn(), canGoBack: () => false, replace: jest.fn(), setParams: jest.fn() },
    Stack: { Screen: () => null },
    useFocusEffect: (callback: () => void | (() => void)) => useEffect(callback, [callback]),
    useLocalSearchParams: () => ({ agentId: "agent_a", name: "Friday" }),
  };
});

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user_a", getToken: jest.fn(async () => "token") }),
}));

jest.mock("expo-crypto", () => ({ randomUUID: () => "client-message-1" }));

const mockRoom = {
  roomId: "room_a", name: null, kind: "dm", roomRevision: 1,
  lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0,
  updatedAt: "2026-09-09T00:00:00.000Z", deliveryKey: "delivery_a",
  members: [{ kind: "agent", id: "agent_a", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "active", joinedAt: "2026-09-09T00:00:00.000Z", joinedSeq: 0 }],
  lastReadSeq: 0, createdAt: "2026-09-09T00:00:00.000Z",
};
const mockSendMessage = jest.fn(async (_roomId: string, request: { clientMessageId: string; text: string }) => ({
  messageId: request.clientMessageId, roomId: "room_a", messageSeq: 1, roomRevision: 1,
  author: { kind: "human", id: "user_a", displayName: "You" }, text: request.text,
  mentions: [], replyToMessageId: null, threadRootMessageId: null,
  createdAt: "2026-09-09T00:00:00.000Z", runId: "run_1",
}));
const mockAbortRun = jest.fn(async () => ({ runId: "run_1", state: "aborting" }));
let mockLiveConnected = true;
let mockLiveHandler: ((event: unknown) => void) | null = null;
const mockMessaging = {
  listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
  getRoom: jest.fn(async () => mockRoom),
  listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
  markRead: jest.fn(async (_roomId: string, request: { lastReadSeq: number }) => request),
  sendMessage: mockSendMessage,
  abortRun: mockAbortRun,
};
const mockConnectedChannels = ["/rooms/room_a/delivery_a"];
const mockDisconnectedChannels: string[] = [];
const mockClaimActiveRoomSubscription = jest.fn(() => () => {});
const mockOnEvent = (handler: (event: unknown) => void) => {
  mockLiveHandler = handler;
  return () => {};
};
jest.mock("../../../../providers/live-events", () => ({
  useLiveEvents: () => ({
    httpReady: true,
    liveConnected: mockLiveConnected,
    liveChannels: mockLiveConnected ? mockConnectedChannels : mockDisconnectedChannels,
    liveError: null,
    connectionRevision: mockLiveConnected ? 1 : 0,
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

async function settleUpdates() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function startRun() {
  await waitFor(() => expect(mockClaimActiveRoomSubscription).toHaveBeenCalledWith({
    roomId: "room_a",
    deliveryKey: "delivery_a",
  }));
  await act(async () => {
    mockLiveHandler?.({
      type: "room.run.frame",
      payload: {
        roomId: "room_a",
        agentId: "agent_a",
        runId: "run_1",
        frame: {
          type: "run_active",
          agentId: "agent_a",
          runId: "run_1",
          active: true,
        },
      },
    });
  });
  await screen.findByLabelText("Stop");
}

async function terminalFrame() {
  await act(async () => {
    mockLiveHandler?.({
      type: "room.run.frame",
      payload: {
        roomId: "room_a",
        agentId: "agent_a",
        runId: "run_1",
        frame: {
          type: "error",
          agentId: "agent_a",
          runId: "run_1",
          message: "Agent run was cancelled",
        },
      },
    });
  });
  await settleUpdates();
}

describe("AgentChat stop run", () => {
  beforeEach(() => {
    mockSendMessage.mockClear();
    mockAbortRun.mockReset().mockResolvedValue({ runId: "run_1", state: "aborting" });
    mockLiveConnected = true;
    mockLiveHandler = null;
    mockClaimActiveRoomSubscription.mockClear();
  });

  afterEach(async () => {
    await cleanup();
  });

  it("keeps HTTP stop usable through reconnect until a terminal frame", async () => {
    const view = await render(<AgentChat />);
    expect(await screen.findByLabelText("Send")).toBeTruthy();
    await startRun();
    expect(screen.queryByLabelText("Send")).toBeNull();

    fireEvent.press(screen.getByLabelText("Stop"));
    await waitFor(() => expect(mockAbortRun).toHaveBeenCalledWith("run_1"));
    expect(screen.getByLabelText("Stop")).toBeTruthy();

    mockLiveConnected = false;
    await view.rerender(<AgentChat />);
    expect(await screen.findByText("Reconnecting live updates. You can keep sending.")).toBeTruthy();
    expect(screen.getByLabelText("Stop")).not.toBeDisabled();

    mockAbortRun.mockRejectedValueOnce(new Error("Abort unavailable"));
    fireEvent.press(screen.getByLabelText("Stop"));
    expect(await screen.findByText("Abort unavailable")).toBeTruthy();
    expect(screen.getByLabelText("Stop")).toBeTruthy();

    await terminalFrame();
    expect(await screen.findByLabelText("Send")).toBeTruthy();
  });
});
