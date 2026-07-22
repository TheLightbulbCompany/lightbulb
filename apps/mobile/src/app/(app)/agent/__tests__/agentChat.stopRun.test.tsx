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

// Stable mock fns (mock-prefixed so the factory may close over them). The
// captured onChatMessage handler lets tests inject incoming frames.
const mockSend = jest.fn(() => true);
const mockSendChat = jest.fn();
let mockConnected = true;
let chatHandler: ((msg: unknown) => void) | null = null;
jest.mock("../../../../providers/gateway", () => ({
  useGateway: () => ({
    isConnected: mockConnected,
    error: null,
    onChatMessage: (h: (msg: unknown) => void) => {
      chatHandler = h;
      return () => {};
    },
    sendReq: jest.fn(async () => ({})),
    sendChat: mockSendChat,
    send: mockSend,
  }),
}));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  useGatewayRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

async function startRun() {
  fireEvent.changeText(screen.getByPlaceholderText("Ask anything"), "do a thing");
  // Prove the controlled-input update flushed (the Send button enables off it)
  // before pressing — a press on the still-disabled button is silently eaten.
  await screen.findByDisplayValue("do a thing");
  fireEvent.press(screen.getByLabelText("Send"));
  expect(mockSendChat).toHaveBeenCalled();
}

describe("AgentChat stop run", () => {
  beforeEach(() => {
    mockSend.mockClear().mockReturnValue(true);
    mockSendChat.mockClear();
    mockConnected = true;
    chatHandler = null;
  });

  it("swaps Send for a Stop button while a run is active", async () => {
    await render(<AgentChat />);
    expect(screen.queryByLabelText("Stop")).toBeNull();
    await startRun();
    expect(await screen.findByLabelText("Stop")).toBeTruthy();
    expect(screen.queryByLabelText("Send")).toBeNull();
  });

  it("sends an agent_abort carrying the active run's id", async () => {
    await render(<AgentChat />);
    await startRun();
    const runId = mockSendChat.mock.calls[0][2];
    fireEvent.press(await screen.findByLabelText("Stop"));
    expect(mockSend).toHaveBeenCalledWith({
      type: "agent_abort",
      agent_id: "agent_a",
      runId,
    });
  });

  it("keeps the run gate up until the aborted terminal arrives", async () => {
    await render(<AgentChat />);
    await startRun();
    fireEvent.press(await screen.findByLabelText("Stop"));
    // The abort is fire-and-don't-wait-for-terminal: busy releases only when
    // the stream delivers the aborted error frame, not on the button press.
    expect(screen.getByLabelText("Stop")).toBeTruthy();
    act(() => {
      chatHandler?.({
        type: "error",
        agent_id: "agent_a",
        message: "Agent run was cancelled",
      });
    });
    expect(await screen.findByLabelText("Send")).toBeTruthy();
    expect(screen.queryByLabelText("Stop")).toBeNull();
  });

  it("disables Stop while the socket is down", async () => {
    await render(<AgentChat />);
    await startRun();
    await screen.findByLabelText("Stop");
    const runId = mockSendChat.mock.calls[0][2];
    // Socket drops mid-run. A streamed frame re-renders the screen the same
    // way the provider's disconnect state change does in the app; the next
    // render must read the dead socket and gate the button — the raw abort
    // send would be a silent no-op, so Stop must not present as pressable
    // (mirrors the TextInput's editable gating). The act must be awaited:
    // the frame lands outside React's event system, so the scheduled render
    // only flushes across a microtask boundary.
    mockConnected = false;
    await act(async () => {
      chatHandler?.({
        type: "chunk",
        agent_id: "agent_a",
        runId,
        content: "partial",
        append: true,
      });
    });
    expect(await screen.findByText("Reconnecting…")).toBeTruthy();
    expect(screen.getByLabelText("Stop")).toBeDisabled();
  });

  it("surfaces an inline error when the abort frame can't be sent", async () => {
    await render(<AgentChat />);
    await startRun();
    // isConnected still reads true but the socket closed underneath —
    // gateway.send reports the miss via its return value.
    mockSend.mockReturnValue(false);
    fireEvent.press(await screen.findByLabelText("Stop"));
    expect(
      await screen.findByText(/stop not sent/i, { exact: false }),
    ).toBeTruthy();
    // The run gate still only releases on the terminal, never locally.
    expect(screen.getByLabelText("Stop")).toBeTruthy();
  });
});
