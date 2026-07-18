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
jest.mock("../../../providers/gateway", () => ({
  useGateway: () => ({ isConnected: true, error: null }),
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
});
