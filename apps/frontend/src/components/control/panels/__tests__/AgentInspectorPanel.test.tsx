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

describe("AgentInspectorPanel", () => {
  beforeEach(() => {
    agentParam = "agent_42";
    subscribed = true;
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
});
