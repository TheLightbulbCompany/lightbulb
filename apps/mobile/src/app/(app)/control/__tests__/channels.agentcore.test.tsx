import { render, screen } from "@testing-library/react-native";

/**
 * M6 task 10-D, row 34. Channel wiring is M5/LATER on Hermes — the tab drops
 * off the control index once AgentCore is confirmed (see
 * control/__tests__/index.agentcore.test.tsx), and this screen holds its RPC
 * and shows a short notice for a bookmarked/deep-linked visit.
 */

jest.mock("../../../../providers/live-events", () => ({
  useLiveEvents: () => ({
    httpReady: true,
    liveConnected: true,
    connectionRevision: 1,
    rpc: jest.fn(),
    getOperation: jest.fn(),
    onEvent: () => () => {},
  }),
}));

// jest's mock-hoisting guard only allows `mock`-prefixed free variables inside
// a jest.mock() factory — hence the naming here, not stylistic preference.
let mockGate: "loading" | "hidden" | "shown" = "hidden";
const mockRpcMethods: (string | null)[] = [];

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useHiddenOnAgentCore: () => mockGate,
  useRuntimeRpc: (_live: unknown, method: string | null) => {
    mockRpcMethods.push(method);
    return { data: undefined, error: undefined, isLoading: false, mutate: jest.fn() };
  },
}));

import ChannelsPanel from "../channels";

describe("mobile Channels tab on an AgentCore workspace", () => {
  beforeEach(() => {
    mockRpcMethods.length = 0;
    mockGate = "hidden";
  });

  it("renders a short notice and fires no RPC once AgentCore is confirmed", async () => {
    await render(<ChannelsPanel />);
    expect(screen.getByText(/isn.t available on this runtime/i)).toBeTruthy();
    expect(mockRpcMethods.every((m) => m === null)).toBe(true);
  });

  it("holds the RPC while the host isn't known yet", async () => {
    mockGate = "loading";
    await render(<ChannelsPanel />);
    expect(mockRpcMethods.every((m) => m === null)).toBe(true);
    expect(screen.queryByText(/isn.t available on this runtime/i)).toBeNull();
  });

  it("still probes on ECS", async () => {
    mockGate = "shown";
    await render(<ChannelsPanel />);
    expect(mockRpcMethods).toContain("channels.status");
  });
});
