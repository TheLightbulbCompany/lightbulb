import { fireEvent, render, screen } from "@testing-library/react-native";

// expo-router isn't in the jest transform allowlist; stub the bits <Chat/> uses.
jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

// Mock the gateway provider + the agents.list RPC so <Chat/> renders offline.
jest.mock("../../../providers/gateway", () => ({
  useGateway: () => ({ isConnected: true, error: null }),
}));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  useGatewayRpc: () => ({
    data: {
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

describe("Chat roster", () => {
  it("renders the agent grid and the New task action", async () => {
    await render(<Chat />);
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(screen.getByText("Atlas")).toBeTruthy();
    expect(screen.getByText("New task")).toBeTruthy();
  });

  it("opens the agent picker when New task is pressed", async () => {
    await render(<Chat />);
    expect(screen.queryByText("START A NEW TASK WITH…")).toBeNull();
    fireEvent.press(screen.getByText("New task"));
    expect(await screen.findByText("START A NEW TASK WITH…")).toBeTruthy();
  });
});
