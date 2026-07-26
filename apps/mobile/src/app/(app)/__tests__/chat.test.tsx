import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";

// expo-router isn't in the jest transform allowlist; stub the bits <Chat/> uses.
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

// Per-test Clerk user (factory is lazy — the closure reads this at render
// time). null = signed-out, keeping the first-run redirect effect inert.
let mockClerkUser: unknown = null;
jest.mock("@clerk/clerk-expo", () => ({
  useUser: () => ({ user: mockClerkUser }),
}));

// Mock the gateway provider + the agents.list RPC so <Chat/> renders offline.
// sendReq backs the reused SessionsSheet's sessions.list fetch; the payload is
// swapped per test (mock-prefixed so the jest.mock factory may reference it).
let mockSessionsPayload: unknown = { sessions: [] };
const mockSendReq = jest.fn(async (method: string) => {
  if (method === "sessions.list") return mockSessionsPayload;
  return {};
});
jest.mock("../../../providers/gateway", () => ({
  useGateway: () => ({ isConnected: true, error: null, sendReq: mockSendReq }),
}));

// Container status drives the first-run redirect's durable stand-down gate
// (`onboarded`). null = no ready status yet (the default; first-run users see
// onboarded=false and get redirected once).
let mockContainerStatus: unknown = null;
jest.mock("../../../lib/containerStatusContext", () => ({
  useContainerStatusContext: () => mockContainerStatus,
}));

// The roster now arrives via the SHARED useAgents hook (the same one the web
// sidebar uses), so that is the seam these tests stub — not the raw
// agents.list RPC underneath it.
const ROSTER_AGENTS = [
  { id: "agent_a", name: "Friday", identity: { emoji: "🤖" } },
  { id: "agent_b", name: "Atlas" },
];
type RosterResult = ReturnType<typeof makeRoster>;
function makeRoster(over: Partial<Record<string, unknown>> = {}) {
  return {
    agents: ROSTER_AGENTS,
    defaultId: "agent_a" as string | undefined,
    error: null as Error | null,
    isLoading: false,
    isSettled: true,
    refresh: jest.fn(),
    createAgent: jest.fn(),
    deleteAgent: jest.fn(),
    updateAgent: jest.fn(),
    ...over,
  };
}
let mockAgents: RosterResult = makeRoster();
jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  useAgents: () => mockAgents,
}));

import Chat from "../chat";
import { GatewayRpcError, WARMING_GRACE_MS } from "@isol8/shared";

const routerPush = jest.requireMock("expo-router").router.push as jest.Mock;

describe("Chat roster", () => {
  beforeEach(() => {
    mockClerkUser = null;
    routerPush.mockClear();
    mockSendReq.mockClear();
    mockSessionsPayload = { sessions: [] };
    mockContainerStatus = null;
    mockAgents = makeRoster();
  });

  it("renders the agent grid and the New task action", async () => {
    await render(<Chat />);
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(screen.getByText("Atlas")).toBeTruthy();
    expect(screen.getByText("New task")).toBeTruthy();
    expect(routerPush).not.toHaveBeenCalled();
  });

  it("opens the agent picker when New task is pressed", async () => {
    await render(<Chat />);
    expect(screen.queryByText("START A NEW TASK WITH…")).toBeNull();
    fireEvent.press(screen.getByText("New task"));
    expect(await screen.findByText("START A NEW TASK WITH…")).toBeTruthy();
  });

  it("walks a first-run user into the default agent's chat", async () => {
    mockClerkUser = { unsafeMetadata: { firstRun: { briefSubmitted: true } } };
    await render(<Chat />);
    await waitFor(() =>
      expect(routerPush).toHaveBeenCalledWith({
        pathname: "/(app)/agent/[agentId]",
        params: { agentId: "agent_a", name: "Friday" },
      }),
    );
    expect(routerPush).toHaveBeenCalledTimes(1);
  });

  it("does NOT redirect a returning user who already onboarded", async () => {
    // briefSubmitted is permanently sticky; the durable `onboarded` fact is
    // what stands the redirect down once the backend reports funnel complete.
    // Without this gate the user gets yanked into Friday on every roster mount.
    mockClerkUser = { unsafeMetadata: { firstRun: { briefSubmitted: true } } };
    mockContainerStatus = { kind: "ready", onboarded: true };
    await render(<Chat />);
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(routerPush).not.toHaveBeenCalled();
  });

  // A container waking from zero answers agents.list with a RETRYABLE error,
  // and useGatewayRpc is already re-probing it every 3s. Shipping that as a
  // red "Couldn't load agents" card told a first-time TestFlight user their
  // app was broken while it was busy working.
  describe("a waking container", () => {
    const WAKING = () =>
      new GatewayRpcError({
        code: "UNAVAILABLE",
        message: "Container is starting up. Try again in a moment.",
        retryable: true,
        retryAfterMs: 3000,
      });

    it("reads as loading, not as a failure", async () => {
      mockAgents = makeRoster({
        agents: [],
        defaultId: undefined,
        error: WAKING(),
        isSettled: false,
      });
      await render(<Chat />);
      expect(screen.getByText("Waking your agents…")).toBeTruthy();
      expect(screen.queryByText(/Couldn’t load agents/)).toBeNull();
    });

    it("still shows a cached roster instead of blanking it", async () => {
      mockAgents = makeRoster({ error: WAKING() });
      await render(<Chat />);
      expect(screen.getByText("Friday")).toBeTruthy();
      expect(screen.queryByText(/Couldn’t load agents/)).toBeNull();
    });

    it("does report a failure that will NOT clear on its own", async () => {
      // NOT "No container provisioned." — useGatewayRpc's fetcher converts
      // that one string into a SUCCESS (data === undefined), so a test using
      // it pins a state production can never reach and would keep passing
      // even if the hard-error path broke entirely.
      mockAgents = makeRoster({
        agents: [],
        defaultId: undefined,
        error: new Error("Gateway handshake rejected"),
        isSettled: false,
      });
      await render(<Chat />);
      expect(screen.getByText(/Gateway handshake rejected/)).toBeTruthy();
      expect(screen.queryByText("Waking your agents…")).toBeNull();
    });

    it("stops calling it 'waking' once the grace window closes", async () => {
      // `retryable` is a hint, not proof of life: the same backend answer
      // covers a container that is waking and one that died. Past the window
      // the user must get the message and a way out.
      jest.useFakeTimers();
      try {
        mockAgents = makeRoster({
          agents: [],
          defaultId: undefined,
          error: WAKING(),
          isSettled: false,
        });
        const view = await render(<Chat />);
        expect(screen.getByText("Waking your agents…")).toBeTruthy();

        jest.setSystemTime(Date.now() + WARMING_GRACE_MS + 1);
        // SWR's 3s error-retry cadence is what re-renders in production.
        mockAgents = makeRoster({
          agents: [],
          defaultId: undefined,
          error: WAKING(),
          isSettled: false,
        });
        await act(async () => {
          view.rerender(<Chat />);
        });

        expect(screen.queryByText("Waking your agents…")).toBeNull();
        expect(screen.getByText(/Container is starting up/)).toBeTruthy();
      } finally {
        jest.useRealTimers();
      }
    });

    it("never shows the error card and 'No agents yet.' at once", async () => {
      mockAgents = makeRoster({
        agents: [],
        defaultId: undefined,
        error: new Error("Gateway handshake rejected"),
        isSettled: true,
      });
      await render(<Chat />);
      expect(screen.getByText(/Gateway handshake rejected/)).toBeTruthy();
      expect(screen.queryByText("No agents yet.")).toBeNull();
    });
  });

  describe("per-agent sessions (long-press)", () => {
    const NAMED = {
      key: "agent:agent_a:user_x:11111111-1111-4111-8111-111111111111",
      label: "Plan the launch",
      updatedAt: 1000,
    };

    it("opens the agent's sessions sheet on long-press", async () => {
      mockClerkUser = { id: "user_x" };
      await render(<Chat />);
      expect(screen.queryByText("SESSIONS")).toBeNull();
      fireEvent(screen.getByLabelText("Friday"), "longPress");
      expect(await screen.findByText("SESSIONS")).toBeTruthy();
    });

    it("navigates into a chosen session with its id + label", async () => {
      mockClerkUser = { id: "user_x" };
      mockSessionsPayload = { sessions: [NAMED] };
      await render(<Chat />);
      fireEvent(screen.getByLabelText("Friday"), "longPress");
      fireEvent.press(await screen.findByText("Plan the launch"));
      expect(routerPush).toHaveBeenCalledWith({
        pathname: "/(app)/agent/[agentId]",
        params: {
          agentId: "agent_a",
          name: "Friday",
          sessionId: "11111111-1111-4111-8111-111111111111",
          sessionLabel: "Plan the launch",
        },
      });
    });

    it("navigates to General without a session id", async () => {
      mockClerkUser = { id: "user_x" };
      await render(<Chat />);
      fireEvent(screen.getByLabelText("Friday"), "longPress");
      fireEvent.press(await screen.findByText("General"));
      expect(routerPush).toHaveBeenCalledWith({
        pathname: "/(app)/agent/[agentId]",
        params: { agentId: "agent_a", name: "Friday" },
      });
    });

    it("starts a new session from the roster sheet", async () => {
      mockClerkUser = { id: "user_x" };
      await render(<Chat />);
      fireEvent(screen.getByLabelText("Friday"), "longPress");
      fireEvent.press(await screen.findByLabelText("New session"));
      expect(routerPush).toHaveBeenCalledWith({
        pathname: "/(app)/agent/[agentId]",
        params: { agentId: "agent_a", name: "Friday", newSession: "1" },
      });
    });

    it("still opens General chat on a normal tap", async () => {
      mockClerkUser = { id: "user_x" };
      await render(<Chat />);
      fireEvent.press(screen.getByLabelText("Friday"));
      expect(routerPush).toHaveBeenCalledWith({
        pathname: "/(app)/agent/[agentId]",
        params: { agentId: "agent_a", name: "Friday" },
      });
    });
  });
});
