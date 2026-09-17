import { Alert } from "react-native";
import { fireEvent, render, screen } from "@testing-library/react-native";
import type { ReactNode } from "react";

// The control Agents tab and the Chat roster answer the same question — "is
// this a wake or a failure?" — and reviewers caught them diverging twice: once
// when only the roster suppressed the card, once when only the roster kept a
// cached list. Both times the control tab had NO tests, so nothing objected.
// These pin the shared contract on this side of it.

jest.mock("expo-router", () => ({
  router: { push: jest.fn(), back: jest.fn() },
  // Real react-navigation renders `options.headerRight` inside the native
  // header; this mock is the whole navigator here, so it has to do that
  // itself, or the "New agent" button would never mount at all.
  Stack: {
    Screen: ({ options }: { options?: { headerRight?: () => ReactNode } }) =>
      options?.headerRight ? options.headerRight() : null,
  },
}));

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

let mockContainerStatus: unknown = { kind: "ready" };
jest.mock("../../../../lib/containerStatusContext", () => ({
  useContainerStatusContext: () => mockContainerStatus,
}));

const ROSTER_AGENTS = [
  { id: "agent_a", name: "Friday" },
  { id: "agent_b", name: "Atlas" },
];
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
let mockAgents = makeRoster();
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  useAgents: () => mockAgents,
  useRuntimeRpcMutation: () => jest.fn(),
}));

import AgentsPanel from "../agents";

describe("control Agents tab roster states", () => {
  beforeEach(() => {
    mockContainerStatus = { kind: "ready" };
    mockAgents = makeRoster();
  });

  it("reads a coming-up container as loading, not as a failure", async () => {
    mockContainerStatus = { kind: "starting" };
    mockAgents = makeRoster({
      agents: [],
      defaultId: undefined,
      error: new Error("Container is starting up. Try again in a moment."),
      isSettled: false,
    });
    await render(<AgentsPanel />);
    expect(screen.getByText("Waking your agents…")).toBeTruthy();
    expect(screen.queryByText(/Couldn’t load agents/)).toBeNull();
  });

  it("reports the failure once the server says the container is ready", async () => {
    mockAgents = makeRoster({
      agents: [],
      defaultId: undefined,
      error: new Error("Gateway handshake rejected"),
      isSettled: false,
    });
    await render(<AgentsPanel />);
    expect(screen.getByText(/Gateway handshake rejected/)).toBeTruthy();
    expect(screen.queryByText("Waking your agents…")).toBeNull();
  });

  it("keeps a populated roster when a revalidation hard-fails", async () => {
    // The card rides ABOVE the tiles. Losing the list, the delete affordances
    // and the New-agent entry point to a transient failure is a worse answer
    // than showing both — and this is exactly where the two tabs diverged.
    mockAgents = makeRoster({ error: new Error("Gateway connection lost") });
    await render(<AgentsPanel />);
    expect(screen.getByText("Friday")).toBeTruthy();
    expect(screen.getByText(/Gateway connection lost/)).toBeTruthy();
  });

  it("lets the backend decide whether the current default can be deleted", async () => {
    const alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    await render(<AgentsPanel />);

    fireEvent(screen.getByLabelText(/Friday.*long-press to delete/i), "longPress");

    expect(alert).toHaveBeenCalledWith(
      "Delete Friday?",
      expect.any(String),
      expect.any(Array),
    );
  });

  it("never shows the error card and 'No agents yet.' at once", async () => {
    mockAgents = makeRoster({
      agents: [],
      defaultId: undefined,
      error: new Error("Gateway handshake rejected"),
      isSettled: true,
    });
    await render(<AgentsPanel />);
    expect(screen.getByText(/Gateway handshake rejected/)).toBeTruthy();
    expect(screen.queryByText("No agents yet.")).toBeNull();
  });

  it("creates an agent without baking in an EFS workspace path", async () => {
    const createAgent = jest.fn().mockResolvedValue(undefined);
    mockAgents = makeRoster({ createAgent });
    await render(<AgentsPanel />);

    await fireEvent.press(screen.getByLabelText("New agent"));
    await fireEvent.changeText(screen.getByPlaceholderText("e.g. Research Assistant"), "Ada");
    await fireEvent.press(screen.getByLabelText("Create agent"));

    expect(createAgent).toHaveBeenCalledWith(
      expect.not.objectContaining({ workspace: expect.anything() }),
    );
    expect(JSON.stringify(createAgent.mock.calls)).not.toContain(".openclaw");
  });
});
