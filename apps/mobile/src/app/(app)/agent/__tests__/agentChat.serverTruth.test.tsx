import { act, render, screen } from "@testing-library/react-native";

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
type LiveHandler = (event: Record<string, unknown>) => void;
let mockLiveHandler: LiveHandler | null = null;
const mockMessaging = {
  listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
  getRoom: jest.fn(async () => mockRoom),
  listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
  markRead: jest.fn(),
  sendMessage: jest.fn(),
  abortRun: jest.fn(),
};
jest.mock("../../../../providers/live-events", () => ({
  useLiveEvents: () => ({
    httpReady: true,
    liveConnected: true,
    liveChannels: ["/rooms/room_a/delivery_a"],
    liveError: null,
    connectionRevision: 1,
    messaging: mockMessaging,
    setActiveRoomSubscription: jest.fn(),
    onEvent: (handler: LiveHandler) => {
      mockLiveHandler = handler;
      return () => { mockLiveHandler = null; };
    },
  }),
}));

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useRuntimeRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

function frame(msg: Record<string, unknown>) {
  const runId = typeof msg.runId === "string" ? msg.runId : "active-run";
  const payloadFrame = { agentId: "agent_a", ...msg };
  return act(async () => {
    mockLiveHandler?.({
      type: "room.run.frame",
      payload: { roomId: "room_a", agentId: "agent_a", runId, frame: payloadFrame },
    });
  });
}

describe("AgentChat server-truth run state", () => {
  beforeEach(() => {
    mockLiveHandler = null;
  });

  it("run_active marks busy and releases on active:false", async () => {
    render(<AgentChat />);
    expect(await screen.findByLabelText("Send")).toBeTruthy();

    await frame({ type: "run_active", active: true });
    expect(screen.getByLabelText("Stop")).toBeTruthy();

    await frame({ type: "run_active", active: false });
    expect(screen.getByLabelText("Send")).toBeTruthy();
  });

  it("adopts an externally-started run and ignores late terminal frames", async () => {
    render(<AgentChat />);
    await screen.findByLabelText("Send");

    await frame({ type: "chunk", runId: "ext-run", content: "hello from web", append: true });
    expect(screen.getByText("hello from web")).toBeTruthy();
    expect(screen.getByLabelText("Stop")).toBeTruthy();

    await frame({ type: "run_ended", runId: "ext-run", outcome: "done" });
    expect(screen.getByLabelText("Send")).toBeTruthy();

    await frame({ type: "chunk", runId: "ext-run", content: "late straggler", append: true });
    expect(screen.getByLabelText("Send")).toBeTruthy();
    expect(screen.queryByText("late straggler")).toBeNull();
  });

  it("agent_busy is a benign no-op", async () => {
    render(<AgentChat />);
    await screen.findByLabelText("Send");
    await frame({ type: "run_active", active: true });
    await frame({ type: "error", code: "agent_busy", message: "Agent is busy" });
    expect(screen.queryByText("Agent is busy")).toBeNull();
    expect(screen.getByLabelText("Stop")).toBeTruthy();
  });

  it("container_waking renders calm text and releases busy", async () => {
    render(<AgentChat />);
    await screen.findByLabelText("Send");
    await frame({ type: "run_active", active: true });
    await frame({ type: "error", code: "container_waking", message: "Waking your agent — this can take a minute." });
    expect(screen.getByText("Waking your agent — this can take a minute.")).toBeTruthy();
    expect(screen.getByLabelText("Send")).toBeTruthy();
  });
});
