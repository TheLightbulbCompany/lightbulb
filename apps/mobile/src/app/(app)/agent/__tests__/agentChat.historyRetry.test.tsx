import { act, render, screen } from "@testing-library/react-native";
import { GatewayRpcError, WARMING_GRACE_MS } from "@isol8/shared";

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
  // Mutable so a test can drive the reconnect edge the history effect keys on.
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
    mockGateway.isConnected = true;
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

  it("gives up when the window closes, with a message free of wire internals", async () => {
    mockSendReq.mockRejectedValue(REBUILDING());

    await render(<AgentChat />);
    await flush();

    // Still riding it out well inside the window — no card yet.
    await tick();
    await tick();
    expect(screen.queryByText(/Couldn’t load history/)).toBeNull();

    // Past the window it must stop and say something actionable.
    jest.setSystemTime(Date.now() + WARMING_GRACE_MS + 1);
    await tick();

    // The card carries the sentence, not the code or the details JSON.
    expect(screen.getByText(/session history is rebuilding/)).toBeTruthy();
    expect(screen.queryByText(/UNAVAILABLE/)).toBeNull();
    expect(screen.queryByText(/"method"/)).toBeNull();

    // And it stops there — a closed window must not keep re-firing.
    const settled = mockSendReq.mock.calls.length;
    await tick();
    expect(mockSendReq).toHaveBeenCalledTimes(settled);
  });

  it("re-opens the silent window after a success, not just on a manual retry", async () => {
    // The budget used to be spent permanently, so the SECOND transient in a
    // long-lived session went straight to the card this path exists to avoid.
    mockSendReq
      .mockRejectedValueOnce(REBUILDING())
      .mockResolvedValueOnce(HISTORY)
      .mockRejectedValueOnce(REBUILDING())
      .mockResolvedValueOnce(HISTORY);

    const view = await render(<AgentChat />);
    await flush();
    await tick();
    expect(screen.getByText(/the transcript survived/)).toBeTruthy();

    // The user stays on this session a long time — long enough that a window
    // opened by the FIRST transient would have expired. Without the
    // reset-on-success the stale deadline is now in the past, so the second
    // transient skips the silent retry and paints the card immediately.
    jest.setSystemTime(Date.now() + WARMING_GRACE_MS + 1);

    // A later reconnect re-fires the effect and hits another transient.
    await act(async () => {
      mockGateway.isConnected = false;
      view.rerender(<AgentChat />);
    });
    await act(async () => {
      mockGateway.isConnected = true;
      view.rerender(<AgentChat />);
    });
    await flush();
    expect(screen.queryByText(/Couldn’t load history/)).toBeNull();
    await tick();
    expect(screen.getByText(/the transcript survived/)).toBeTruthy();
    expect(screen.queryByText(/Couldn’t load history/)).toBeNull();
  });

  it("shows a non-retryable failure immediately — no silent waiting", async () => {
    mockSendReq.mockRejectedValue(new Error("Gateway connection lost"));

    await render(<AgentChat />);
    await flush();

    expect(screen.getByText(/Gateway connection lost/)).toBeTruthy();
    expect(mockSendReq).toHaveBeenCalledTimes(1);
  });
});
