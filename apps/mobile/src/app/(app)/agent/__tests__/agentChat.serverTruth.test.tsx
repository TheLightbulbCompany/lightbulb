import { act, render, screen } from "@testing-library/react-native";

// Server-truth run state + friendly-error handling (web useAgentChat parity):
//  - `run_active` marks/releases busy independent of any locally-started run,
//    so a run started from web/cron/another device can't read as idle.
//  - frames for an externally-started run are ADOPTED and rendered.
//  - `agent_busy` is a benign no-op; `container_waking` renders calm plain
//    text (not an error card) while still releasing the busy gate.

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
  useUser: () => ({ user: null }),
  useAuth: () => ({ getToken: jest.fn(async () => "token") }),
}));

let mockUuidCounter = 0;
jest.mock("expo-crypto", () => ({
  randomUUID: () => `00000000-0000-4000-8000-${String(++mockUuidCounter).padStart(12, "0")}`,
}));

type ChatHandler = (msg: Record<string, unknown>) => void;
let mockChatHandler: ChatHandler | null = null;

jest.mock("../../../../providers/gateway", () => ({
  useGateway: () => ({
    isConnected: true,
    error: null,
    onChatMessage: (handler: ChatHandler) => {
      mockChatHandler = handler;
      return () => {
        mockChatHandler = null;
      };
    },
    sendReq: jest.fn(async () => ({})),
    sendChat: jest.fn(),
    send: jest.fn(() => true),
  }),
}));

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useGatewayRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

// NOTE: this RNTL version's act() is async under the hood — an un-awaited
// act starts a scope the next statement races, silently dropping the state
// update (see ByoKeyScreen.test.tsx). Always `await frame(...)`.
function frame(msg: Record<string, unknown>) {
  return act(async () => {
    mockChatHandler?.({ agent_id: "agent_a", ...msg });
  });
}

describe("AgentChat server-truth run state", () => {
  beforeEach(() => {
    mockChatHandler = null;
  });

  it("run_active marks busy (Stop shown) and releases on active:false", async () => {
    await render(<AgentChat />);
    expect(screen.getByLabelText("Send")).toBeTruthy();

    await frame({ type: "run_active", active: true });
    expect(screen.getByLabelText("Stop")).toBeTruthy();

    await frame({ type: "run_active", active: false });
    expect(screen.getByLabelText("Send")).toBeTruthy();
  });

  it("adopts an externally-started run: renders its frames and holds busy", async () => {
    await render(<AgentChat />);

    await frame({ type: "chunk", runId: "ext-run", content: "hello from web", append: true });
    expect(screen.getByText("hello from web")).toBeTruthy();
    expect(screen.getByLabelText("Stop")).toBeTruthy();

    await frame({ type: "run_ended", runId: "ext-run", outcome: "done" });
    expect(screen.getByLabelText("Send")).toBeTruthy();

    // A stray late frame for the ended run must NOT re-arm the busy gate.
    await frame({ type: "chunk", runId: "ext-run", content: "late straggler", append: true });
    expect(screen.getByLabelText("Send")).toBeTruthy();
    expect(screen.queryByText("late straggler")).toBeNull();
  });

  it("agent_busy is a benign no-op (no card, busy untouched)", async () => {
    await render(<AgentChat />);
    await frame({ type: "run_active", active: true });

    await frame({ type: "error", code: "agent_busy", message: "Agent is busy" });
    expect(screen.queryByText("Agent is busy")).toBeNull();
    expect(screen.getByLabelText("Stop")).toBeTruthy();
  });

  it("container_waking renders calm text and releases busy", async () => {
    await render(<AgentChat />);
    await frame({ type: "run_active", active: true });

    await frame({
      type: "error",
      code: "container_waking",
      message: "Waking your agent — this can take a minute.",
    });
    expect(
      screen.getByText("Waking your agent — this can take a minute."),
    ).toBeTruthy();
    // Calm terminal: gate released, composer back to Send.
    expect(screen.getByLabelText("Send")).toBeTruthy();
  });
});
