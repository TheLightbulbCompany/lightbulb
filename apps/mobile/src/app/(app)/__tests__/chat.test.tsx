import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

// expo-router isn't in the jest transform allowlist; stub the bits <Chat/> uses.
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

// Per-test Clerk user (factory is lazy — the closure reads this at render
// time). null = signed-out, keeping the first-run redirect effect inert.
let mockClerkUser: unknown = null;
jest.mock("@clerk/clerk-expo", () => ({
  useUser: () => ({ user: mockClerkUser }),
}));

// The roster reads through the canonical runtime source.
jest.mock("../../../providers/live-events", () => ({
  useLiveEvents: () => ({
    httpReady: true,
    liveConnected: true,
    liveError: null,
    connectionRevision: 1,
    rpc: jest.fn(),
    getOperation: jest.fn(),
    onEvent: () => () => {},
  }),
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
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useAgents: () => mockAgents,
}));

import Chat from "../chat";
import { RuntimeRpcError } from "@lightbulb/shared";

const routerPush = jest.requireMock("expo-router").router.push as jest.Mock;

describe("Chat roster", () => {
  beforeEach(() => {
    mockClerkUser = null;
    routerPush.mockClear();
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

  // Whether a missing roster is a wake or a failure is decided by the SERVER's
  // container status, not by the shape of the RPC error. An earlier revision
  // inferred it from the error plus a 90s timer; the timer's deadline lived in
  // a component ref that reset on every park/unpark and remount, so a dead
  // container re-armed the grace forever.
  describe("a waking container", () => {
    const WAKING = () =>
      new RuntimeRpcError({
        status: 503,
        message: "Container is starting up. Try again in a moment.",
        retryable: true,
        retryAfter: 3,
      });

    it("reads as loading while the server says the container is coming up", async () => {
      mockContainerStatus = { kind: "starting" };
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

    it("says so during a first run, where there is NO error to classify", async () => {
      // "No container provisioned." is swallowed into undefined-as-success by
      // the shared fetcher, so this path has no error at all. Driving the
      // state off the error left it skeletoning silently forever.
      mockContainerStatus = { kind: "starting" };
      mockAgents = makeRoster({
        agents: [],
        defaultId: undefined,
        error: null,
        isSettled: false,
      });
      await render(<Chat />);
      expect(screen.getByText("Waking your agents…")).toBeTruthy();
    });

    it("reports the failure IMMEDIATELY once the server says ready", async () => {
      // No grace period: the container is up, so the RPC failing is real and
      // waiting 90s to admit it only makes the genuine error case worse.
      mockContainerStatus = { kind: "ready" };
      mockAgents = makeRoster({
        agents: [],
        defaultId: undefined,
        error: WAKING(),
        isSettled: false,
      });
      await render(<Chat />);
      expect(screen.getByText(/Container is starting up/)).toBeTruthy();
      expect(screen.queryByText("Waking your agents…")).toBeNull();
    });

    it("gives the user a way out of the error card", async () => {
      mockContainerStatus = { kind: "ready" };
      const refresh = jest.fn();
      mockAgents = makeRoster({
        agents: [],
        defaultId: undefined,
        error: new Error("Gateway handshake rejected"),
        isSettled: false,
        refresh,
      });
      await render(<Chat />);
      fireEvent.press(screen.getByLabelText("Retry loading agents"));
      expect(refresh).toHaveBeenCalled();
    });

    it("still shows a cached roster instead of blanking it", async () => {
      mockContainerStatus = { kind: "starting" };
      mockAgents = makeRoster({ error: WAKING() });
      await render(<Chat />);
      expect(screen.getByText("Friday")).toBeTruthy();
      expect(screen.queryByText(/Couldn’t load agents/)).toBeNull();
    });

    it("never shows the error card and 'No agents yet.' at once", async () => {
      mockContainerStatus = { kind: "ready" };
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

  it("opens the agent's canonical room on tap", async () => {
    mockClerkUser = { id: "user_x" };
    await render(<Chat />);
    fireEvent.press(screen.getByLabelText("Friday"));
    expect(routerPush).toHaveBeenCalledWith({
      pathname: "/(app)/agent/[agentId]",
      params: { agentId: "agent_a", name: "Friday" },
    });
  });
});
