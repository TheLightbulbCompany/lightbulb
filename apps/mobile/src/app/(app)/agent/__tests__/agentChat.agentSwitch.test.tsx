import { act, fireEvent, render, screen } from "@testing-library/react-native";

// expo-router isn't in the jest transform allowlist; stub what AgentChat uses.
// mockParams is mutable so a test can navigate the persisted screen to another
// agent the way expo-router does: same component, new params.
let mockParams: Record<string, string | undefined> = {
  agentId: "agent_a",
  name: "Friday",
};
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
  useLocalSearchParams: () => mockParams,
}));

// user: null keeps history hydration inert — which is exactly the trap this
// suite pins: nothing else may be relied on to clear a stale conversation.
jest.mock("@clerk/clerk-expo", () => ({ useUser: () => ({ user: null }) }));

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

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  useGatewayRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

async function converseAsFriday(): Promise<string> {
  fireEvent.changeText(
    screen.getByPlaceholderText("Ask anything"),
    "friday question",
  );
  await screen.findByDisplayValue("friday question");
  fireEvent.press(screen.getByLabelText("Send"));
  const runId = mockSendChat.mock.calls[0][2] as string;
  act(() => {
    chatHandler?.({
      type: "chunk",
      agent_id: "agent_a",
      runId,
      content: "The launch answer from Friday.",
      append: true,
    });
    chatHandler?.({ type: "run_ended", agent_id: "agent_a", runId });
  });
  await screen.findByText(/launch answer from Friday/);
  return runId;
}

describe("AgentChat agent switching", () => {
  beforeEach(() => {
    mockParams = { agentId: "agent_a", name: "Friday" };
    mockSendChat.mockClear();
    chatHandler = null;
  });

  it("never shows the previous agent's conversation after switching", async () => {
    const view = await render(<AgentChat />);
    await converseAsFriday();

    // Navigate the persisted screen to Atlas — new params, same component.
    mockParams = { agentId: "agent_b", name: "Atlas" };
    await view.rerender(<AgentChat />);

    // Friday's transcript must be gone IMMEDIATELY — not "once Atlas's
    // history fetch replaces it" (here it never fetches: no user). This is
    // the stale-conversation flash: the General→General switch hit
    // switchSession's same-session early return and cleared nothing.
    expect(screen.queryByText(/launch answer from Friday/)).toBeNull();
    expect(screen.queryByText("friday question")).toBeNull();
    expect(screen.getByText(/Start a conversation/)).toBeTruthy();
  });

  it("drops late frames from the previous agent's run after switching", async () => {
    const view = await render(<AgentChat />);
    const runId = await converseAsFriday();

    mockParams = { agentId: "agent_b", name: "Atlas" };
    await view.rerender(<AgentChat />);

    // A straggler frame from Friday's run must not paint into Atlas's
    // timeline (fresh screen state owns no such run).
    act(() => {
      chatHandler?.({
        type: "chunk",
        agent_id: "agent_a",
        runId,
        content: "late friday text",
        append: true,
      });
    });
    expect(screen.queryByText(/late friday text/)).toBeNull();
  });
});
