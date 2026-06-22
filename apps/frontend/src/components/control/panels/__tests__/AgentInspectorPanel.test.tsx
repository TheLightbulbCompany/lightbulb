import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AgentInspectorPanel } from "../AgentInspectorPanel";

// The inspector mounts the SAME per-agent control tabs AgentsPanel does. Stub
// them so we can assert which one is mounted without dragging their gateway /
// billing deps into the sandbox.
vi.mock("../AgentOverviewTab", () => ({
  AgentOverviewTab: ({ agentId }: { agentId: string }) => (
    <div data-testid="overview-tab">overview:{agentId}</div>
  ),
}));
vi.mock("../AgentToolsTab", () => ({
  AgentToolsTab: ({ agentId }: { agentId: string }) => (
    <div data-testid="tools-tab">tools:{agentId}</div>
  ),
}));
vi.mock("../AgentChannelsSection", () => ({
  AgentChannelsSection: ({ agentId }: { agentId: string }) => (
    <div data-testid="channels-tab">channels:{agentId}</div>
  ),
}));

// ?agent drives which agent the inspector inspects.
let agentParam: string | null = "agent_42";
vi.mock("next/navigation", () => ({
  useSearchParams: () => ({ get: (k: string) => (k === "agent" ? agentParam : null) }),
}));

// agents.list feeds the header (name/emoji), mirroring AgentsPanel.
const agentsListData: { defaultId?: string; agents?: Array<{ id: string; name?: string; identity?: { emoji?: string } }> } =
  { defaultId: "main", agents: [{ id: "agent_42", name: "Scout", identity: { emoji: "🔭" } }] };
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (method: string | null) => {
    if (method === "agents.list") {
      return { data: agentsListData, error: undefined, isLoading: false, mutate: vi.fn() };
    }
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
  useGatewayRpcMutation: () => vi.fn(),
}));

// Channels tab only renders for subscribed owners.
let subscribed = true;
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({ isSubscribed: subscribed }),
}));

// The Overview tab folds in the Teams org controls (reports-to + lifecycle).
// Stub AgentOrgControls so we can assert it mounts with the matched roster
// agent without dragging its mutation deps into the sandbox.
vi.mock("@/components/teams/agents/AgentOrgControls", () => ({
  AgentOrgControls: ({ agent }: { agent: { id: string } }) => (
    <div data-testid="org-controls">org:{agent.id}</div>
  ),
}));

// The org section reads the Teams roster via useTeamsApi().read("/agents").
// `rosterData`/`rosterError` drive the graceful-degrade branches.
let rosterData: { agents?: Array<{ id: string; name: string }> } | undefined = {
  agents: [{ id: "agent_42", name: "Scout" }],
};
let rosterError: Error | undefined;
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      path === "/agents"
        ? { data: rosterData, error: rosterError, isLoading: false, mutate: vi.fn() }
        : { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() },
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

describe("AgentInspectorPanel", () => {
  beforeEach(() => {
    agentParam = "agent_42";
    subscribed = true;
    rosterData = { agents: [{ id: "agent_42", name: "Scout" }] };
    rosterError = undefined;
  });

  it("renders the three tab labels", () => {
    render(<AgentInspectorPanel />);
    expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Tools" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Channels" })).toBeInTheDocument();
  });

  it("mounts the Overview tab by default with the ?agent id", () => {
    render(<AgentInspectorPanel />);
    expect(screen.getByTestId("overview-tab")).toHaveTextContent("overview:agent_42");
    expect(screen.queryByTestId("tools-tab")).not.toBeInTheDocument();
  });

  it("switches the mounted tab when a tab is clicked", () => {
    render(<AgentInspectorPanel />);
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(screen.getByTestId("tools-tab")).toHaveTextContent("tools:agent_42");
    expect(screen.queryByTestId("overview-tab")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Channels" }));
    expect(screen.getByTestId("channels-tab")).toHaveTextContent("channels:agent_42");
    expect(screen.queryByTestId("tools-tab")).not.toBeInTheDocument();
  });

  it("marks the active tab with aria-selected for screen readers", () => {
    render(<AgentInspectorPanel />);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Tools" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(screen.getByRole("tab", { name: "Tools" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("renders the agent name in the header", () => {
    render(<AgentInspectorPanel />);
    expect(screen.getByText("Scout")).toBeInTheDocument();
  });

  it("hides the Channels tab for non-subscribed owners", () => {
    subscribed = false;
    render(<AgentInspectorPanel />);
    expect(screen.queryByRole("tab", { name: "Channels" })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Overview" })).toBeInTheDocument();
  });

  it("shows a calm empty state when no ?agent is present", () => {
    agentParam = null;
    render(<AgentInspectorPanel />);
    expect(screen.getByText(/select an agent/i)).toBeInTheDocument();
    expect(screen.queryByTestId("overview-tab")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Overview" })).not.toBeInTheDocument();
  });

  it("renders the org controls when the Teams roster has the ?agent id", () => {
    render(<AgentInspectorPanel />);
    expect(screen.getByTestId("org-controls")).toHaveTextContent("org:agent_42");
  });

  it("renders nothing org-side when the Teams roster errors", () => {
    rosterError = new Error("boom");
    rosterData = undefined;
    render(<AgentInspectorPanel />);
    // Overview tab still mounts; the org section degrades to nothing so a
    // pause/resume POST never fires against a missing Paperclip row.
    expect(screen.getByTestId("overview-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("org-controls")).not.toBeInTheDocument();
  });

  it("renders nothing org-side when the agent is absent from the roster", () => {
    rosterData = { agents: [{ id: "someone_else", name: "Other" }] };
    render(<AgentInspectorPanel />);
    expect(screen.getByTestId("overview-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("org-controls")).not.toBeInTheDocument();
  });

  it("keeps the org section on the Overview tab only", () => {
    render(<AgentInspectorPanel />);
    expect(screen.getByTestId("org-controls")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Tools" }));
    expect(screen.queryByTestId("org-controls")).not.toBeInTheDocument();
  });
});
