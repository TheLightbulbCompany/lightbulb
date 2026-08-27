import { act, fireEvent, render, screen } from "@testing-library/react-native";

// expo-router isn't in the jest transform allowlist; stub what AgentChat uses.
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

// user: null keeps history hydration + first-run kickoff inert.
jest.mock("@clerk/clerk-expo", () => ({
  useUser: () => ({ user: null }),
  useAuth: () => ({ getToken: jest.fn(async () => "token") }),
}));

jest.mock("expo-crypto", () => ({
  randomUUID: () => "00000000-0000-4000-8000-000000000000",
}));

const mockSendChat = jest.fn();
let chatHandler: ((msg: unknown) => void) | null = null;
jest.mock("../../../../providers/gateway", () => ({
  useGateway: () => ({
    isConnected: true,
    error: null,
    onChatMessage: (h: (msg: unknown) => void) => {
      chatHandler = h;
      return () => {};
    },
    sendReq: jest.fn(async () => ({})),
    sendChat: mockSendChat,
    send: jest.fn(() => true),
  }),
}));

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useGatewayRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

async function startRun(): Promise<string> {
  fireEvent.changeText(screen.getByPlaceholderText("Ask anything"), "go");
  await screen.findByDisplayValue("go");
  fireEvent.press(screen.getByLabelText("Send"));
  expect(mockSendChat).toHaveBeenCalled();
  return mockSendChat.mock.calls[0][2] as string; // clientRunId
}

function frame(runId: string, msg: Record<string, unknown>) {
  act(() => {
    chatHandler?.({ agent_id: "agent_a", runId, ...msg });
  });
}

describe("AgentChat stream resync across tool splits", () => {
  beforeEach(() => {
    mockSendChat.mockClear();
    chatHandler = null;
  });

  it("keeps the live segment intact when a segment-scoped resync arrives", async () => {
    await render(<AgentChat />);
    const runId = await startRun();

    // Segment 0 streams, then a tool call freezes it at offset 10.
    frame(runId, { type: "chunk", content: "Part one. ", append: true });
    frame(runId, { type: "tool_start", tool: "exec", toolCallId: "t1" });
    frame(runId, { type: "chunk", content: "Part two begins", append: true });

    // Non-append resync. Upstream resets its text accumulator at every
    // assistant message_start (i.e. after every tool call), so this frame
    // carries ONLY the current segment's text — NOT the run-cumulative
    // buffer. Treating it as run-cumulative chopped the live bubble at the
    // committed offset ("disappearing sentences").
    frame(runId, { type: "chunk", content: "Part two begins and continues." });

    expect(await screen.findByText(/Part two begins and continues\./)).toBeTruthy();
    expect(screen.getByText(/Part one\./)).toBeTruthy();
  });

  it("still accepts a plain full-text resync when no tool has split the run", async () => {
    await render(<AgentChat />);
    const runId = await startRun();
    frame(runId, { type: "chunk", content: "Hello wor", append: true });
    // First-frame-per-connection style resync, no committed prefix.
    frame(runId, { type: "chunk", content: "Hello world." });
    expect(await screen.findByText(/Hello world\./)).toBeTruthy();
  });
});
