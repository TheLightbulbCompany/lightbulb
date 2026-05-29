// apps/frontend/src/__tests__/control/AgentsPanel.deeplink.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const searchParams = new URLSearchParams();

vi.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => ({
    data: { agents: [{ id: "a1", name: "Alpha" }, { id: "a2", name: "Beta" }], defaultId: "a1" },
    error: undefined,
    isLoading: false,
    mutate: vi.fn(),
  }),
  useGatewayRpcMutation: () => vi.fn(),
}));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ isSubscribed: true }) }));
// Child tabs hit RPC of their own — stub them to surface which agent is selected.
vi.mock("@/components/control/panels/AgentOverviewTab", () => ({
  AgentOverviewTab: ({ agentId }: { agentId: string }) => <div>overview:{agentId}</div>,
}));
vi.mock("@/components/control/panels/AgentToolsTab", () => ({ AgentToolsTab: () => <div /> }));
vi.mock("@/components/control/panels/AgentChannelsSection", () => ({ AgentChannelsSection: () => <div /> }));
vi.mock("@/components/control/panels/AgentCreateForm", () => ({ AgentCreateForm: () => <div /> }));

import { AgentsPanel } from "@/components/control/panels/AgentsPanel";

describe("AgentsPanel deep link", () => {
  it("selects the agent named in the ?agent= query param on first render", () => {
    searchParams.set("agent", "a2");
    render(<AgentsPanel />);
    // The detail pane renders the selected agent's overview tab, not the
    // "Select an agent" placeholder.
    expect(screen.getByText("overview:a2")).toBeInTheDocument();
    expect(screen.queryByText("Select an agent to view details")).toBeNull();
  });

  it("shows the placeholder when no ?agent= param is present", () => {
    searchParams.delete("agent");
    render(<AgentsPanel />);
    expect(screen.getByText("Select an agent to view details")).toBeInTheDocument();
  });
});
