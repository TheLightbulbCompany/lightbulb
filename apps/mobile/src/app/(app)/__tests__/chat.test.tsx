import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

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

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  useGatewayRpc: () => ({
    data: {
      defaultId: "agent_a",
      agents: [
        { id: "agent_a", name: "Friday", identity: { emoji: "🤖" } },
        { id: "agent_b", name: "Atlas" },
      ],
    },
    error: null,
    isLoading: false,
  }),
}));

import Chat from "../chat";

const routerPush = jest.requireMock("expo-router").router.push as jest.Mock;

describe("Chat roster", () => {
  beforeEach(() => {
    mockClerkUser = null;
    routerPush.mockClear();
    mockSendReq.mockClear();
    mockSessionsPayload = { sessions: [] };
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

  it("does not redirect once the kickoff has started", async () => {
    mockClerkUser = {
      unsafeMetadata: { firstRun: { briefSubmitted: true, started: true } },
    };
    await render(<Chat />);
    await waitFor(() => expect(routerPush).not.toHaveBeenCalled());
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
