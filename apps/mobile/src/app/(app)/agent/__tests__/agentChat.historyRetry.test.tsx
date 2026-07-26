import { act, render, screen } from "@testing-library/react-native";
import { GatewayRpcError } from "@isol8/shared";

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
  useUser: () => ({ user: { id: "user_1" } }),
  useAuth: () => ({ getToken: jest.fn(async () => "token") }),
}));

jest.mock("expo-crypto", () => ({
  randomUUID: () => "00000000-0000-4000-8000-000000000000",
}));

const mockSendReq = jest.fn();
// One frozen gateway value for the whole suite: the history effect lists
// `sendReq` in its deps, so a per-render identity would re-fire the fetch on
// every render and make the retry counts meaningless.
const mockGateway = {
  isConnected: true,
  error: null,
  onChatMessage: () => () => {},
  sendReq: mockSendReq,
  sendChat: jest.fn(),
  send: jest.fn(() => true),
};
jest.mock("../../../../providers/gateway", () => ({
  useGateway: () => mockGateway,
}));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  useGatewayRpc: () => ({ data: null, error: null, isLoading: false }),
}));

import AgentChat from "../[agentId]";

const HISTORY = {
  messages: [{ role: "assistant", content: "the transcript survived" }],
};

/**
 * OpenClaw answers chat.history with UNAVAILABLE + retryable + retryAfterMs
 * while its session index rebuilds — a ~250ms condition. It reached
 * TestFlight as a red card reading
 *   "Couldn't load history — session history is rebuilding; retry shortly —
 *    [UNAVAILABLE] — {"method":"chat.history"}"
 * with a manual Retry link. Both halves are pinned here: the transient is
 * ridden out silently, and if it never clears the message stays human.
 */
const REBUILDING = () =>
  new GatewayRpcError({
    code: "UNAVAILABLE",
    message: "session history is rebuilding; retry shortly",
    details: { method: "chat.history" },
    retryable: true,
    retryAfterMs: 250,
  });

/** Flush pending promise jobs without letting RNTL's waitFor run the timers
 *  (it auto-advances them under fake timers, which would fire the very retry
 *  these tests are counting). */
const flush = () => act(async () => {});

/** One retry tick: fire the scheduled refetch, then settle it. */
async function tick() {
  await act(async () => {
    jest.advanceTimersByTime(250);
  });
  await flush();
}

describe("AgentChat history retry", () => {
  beforeEach(() => {
    mockSendReq.mockReset();
    jest.useFakeTimers();
  });
  afterEach(() => {
    // Drop the screen's Animated loops before handing the clock back — a
    // pulse left scheduled fires after teardown and takes the worker with it.
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it("rides out a retryable failure without ever showing the user a card", async () => {
    mockSendReq
      .mockRejectedValueOnce(REBUILDING())
      .mockResolvedValueOnce(HISTORY);

    await render(<AgentChat />);
    await flush();

    // The first attempt has already failed — nothing user-visible may appear.
    expect(mockSendReq).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Couldn’t load history/)).toBeNull();
    expect(screen.queryByLabelText("Retry loading history")).toBeNull();

    await tick();

    expect(mockSendReq).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/the transcript survived/)).toBeTruthy();
    expect(screen.queryByText(/Couldn’t load history/)).toBeNull();
  });

  it("gives up after the budget and shows a message free of wire internals", async () => {
    mockSendReq.mockRejectedValue(REBUILDING());

    await render(<AgentChat />);
    await flush();

    // Budget is 3 silent retries; drain them.
    for (let i = 0; i < 3; i++) await tick();
    expect(mockSendReq).toHaveBeenCalledTimes(4);

    // The card carries the sentence, not the code or the details JSON.
    expect(screen.getByText(/session history is rebuilding/)).toBeTruthy();
    expect(screen.queryByText(/UNAVAILABLE/)).toBeNull();
    expect(screen.queryByText(/"method"/)).toBeNull();

    // And it stops there — a spent budget must not keep re-firing.
    await tick();
    expect(mockSendReq).toHaveBeenCalledTimes(4);
  });

  it("shows a non-retryable failure immediately — no silent waiting", async () => {
    mockSendReq.mockRejectedValue(new Error("Gateway connection lost"));

    await render(<AgentChat />);
    await flush();

    expect(screen.getByText(/Gateway connection lost/)).toBeTruthy();
    expect(mockSendReq).toHaveBeenCalledTimes(1);
  });
});
