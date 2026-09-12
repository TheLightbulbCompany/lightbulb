import { act, cleanup, fireEvent, render, screen } from "@testing-library/react-native";

jest.mock("expo-router", () => ({
  router: { push: jest.fn(), back: jest.fn(), canGoBack: () => false, replace: jest.fn(), setParams: jest.fn() },
  Stack: { Screen: () => null },
  useFocusEffect: jest.fn(),
  useLocalSearchParams: () => ({ agentId: "agent_a", name: "Friday" }),
}));

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user_a", getToken: jest.fn(async () => "token") }),
}));

jest.mock("expo-crypto", () => ({
  randomUUID: () => "00000000-0000-4000-8000-000000000000",
}));

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
// The real provider fans every event out to EVERY registered handler; the
// screen and the shared useRooms each register one, so a single-slot mock
// silently drops whichever of them re-subscribed first.
const mockLiveHandlers = new Set<(event: unknown) => void>();
const emitLiveEvent = (event: unknown) => {
  for (const handler of [...mockLiveHandlers]) handler(event);
};
const mockMessaging = {
  listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
  getRoom: jest.fn(async () => mockRoom),
  listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
  markRead: jest.fn(async () => ({ roomId: "room_a", lastReadSeq: 1 })),
  getRunStatus: jest.fn(async () => ({ runId: "run_1", state: "running" })),
  sendMessage: mockSendMessage,
  abortRun: jest.fn(),
};
const mockLiveChannels = ["/rooms/room_a/delivery_a"];
const mockClaimActiveRoomSubscription = jest.fn(() => () => {});
const mockOnEvent = (handler: (event: unknown) => void) => {
  mockLiveHandlers.add(handler);
  return () => mockLiveHandlers.delete(handler);
};
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

async function startRun(): Promise<string> {
  fireEvent.changeText(await screen.findByPlaceholderText("Ask anything"), "go");
  await screen.findByDisplayValue("go");
  fireEvent.press(screen.getByLabelText("Send"));
  await screen.findByText("go");
  expect(mockSendMessage).toHaveBeenCalled();
  return "run_1";
}

async function frame(runId: string, value: Record<string, unknown>) {
  const payloadFrame = { agentId: "agent_a", runId, ...value };
  await act(async () => {
    emitLiveEvent({
      type: "room.run.frame",
      payload: { roomId: "room_a", agentId: "agent_a", runId, frame: payloadFrame },
    });
  });
}

async function finishRun(runId: string) {
  await act(async () => {
    emitLiveEvent({
      type: "room.run.frame",
      payload: {
        roomId: "room_a",
        agentId: "agent_a",
        runId,
        frame: { type: "run_ended", agentId: "agent_a", runId, outcome: "done" },
      },
    });
  });
}

describe("AgentChat stream resync across tool splits", () => {
  beforeEach(() => {
    mockSendMessage.mockClear();
    mockClaimActiveRoomSubscription.mockClear();
    mockLiveHandlers.clear();
  });

  afterEach(async () => {
    await cleanup();
  });

  it("keeps the live segment intact when a segment-scoped resync arrives", async () => {
    await render(<AgentChat />);
    const runId = await startRun();

    await frame(runId, { type: "chunk", content: "Part one. ", append: true });
    await frame(runId, { type: "tool_start", tool: "exec", toolCallId: "t1" });
    await frame(runId, { type: "chunk", content: "Part two begins", append: true });
    await frame(runId, { type: "chunk", content: "Part two begins and continues." });

    expect(await screen.findByText(/Part two begins and continues\./)).toBeTruthy();
    expect(screen.getByText(/Part one\./)).toBeTruthy();
    await finishRun(runId);
  });

  it("accepts a full-text resync when no tool split the run", async () => {
    await render(<AgentChat />);
    const runId = await startRun();
    await frame(runId, { type: "chunk", content: "Hello wor", append: true });
    await frame(runId, { type: "chunk", content: "Hello world." });
    expect(await screen.findByText(/Hello world\./)).toBeTruthy();
    await finishRun(runId);
  });
});
