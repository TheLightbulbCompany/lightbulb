// apps/frontend/src/__tests__/control/AgentsPanel.deeplink.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// AgentsPanel reads its roster through the shared useAgents hook, which
// reaches for the gateway context — stub the hook, not the RPC beneath it.
vi.mock("@/hooks/useAgents", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useAgents")>();
  return {
    ...actual,
    useAgents: () => ({
      agents: [
        { id: "a1", name: "Alpha" },
        { id: "a2", name: "Beta" },
      ],
      defaultId: "a1",
      error: undefined,
      isLoading: false,
      isSettled: true,
      refresh: vi.fn(),
      createAgent: vi.fn(),
      deleteAgent: vi.fn(),
      updateAgent: vi.fn(),
    }),
  };
});

vi.mock("@/hooks/useRuntimeRpc", () => ({
  useRuntimeRpc: () => ({
    data: { agents: [{ id: "a1", name: "Alpha" }, { id: "a2", name: "Beta" }], defaultId: "a1" },
    error: undefined,
    isLoading: false,
    mutate: vi.fn(),
  }),
  useRuntimeRpcMutation: () => vi.fn(),
}));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ isSubscribed: true }) }));
// Child tabs hit RPC of their own — stub them to surface which agent is selected.
vi.mock("@/components/control/panels/AgentOverviewTab", () => ({
  AgentOverviewTab: ({ agentId }: { agentId: string }) => <div>overview:{agentId}</div>,
}));
vi.mock("@/components/control/panels/AgentToolsTab", () => ({ AgentToolsTab: () => <div /> }));
vi.mock("@/components/control/panels/AgentCreateForm", () => ({
  AgentCreateForm: () => <div>create-form</div>,
}));

const mockOpenMarketplace = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ open: mockOpenMarketplace }),
}));

import { AgentsPanel } from "@/components/control/panels/AgentsPanel";

describe("AgentsPanel deep link", () => {
  it("selects the agent named in the initialAgentId prop on first render", () => {
    render(<AgentsPanel initialAgentId="a2" />);
    // The detail pane renders the selected agent's overview tab, not the
    // "Select an agent" placeholder.
    expect(screen.getByText("overview:a2")).toBeInTheDocument();
    expect(screen.queryByText("Select an agent to view details")).toBeNull();
  });

  it("shows the placeholder when no initialAgentId prop is present", () => {
    render(<AgentsPanel />);
    expect(screen.getByText("Select an agent to view details")).toBeInTheDocument();
  });

  it("re-applies a CHANGED initialAgentId on an already-mounted panel", () => {
    // Desktop dock panels stay mounted; a deep link to an open panel arrives
    // as a prop change (updateParameters → panelParams), not a remount — the
    // render-time prev-compare must re-apply it.
    const { rerender } = render(<AgentsPanel initialAgentId="a2" />);
    expect(screen.getByText("overview:a2")).toBeInTheDocument();
    rerender(<AgentsPanel initialAgentId="a1" />);
    expect(screen.getByText("overview:a1")).toBeInTheDocument();
  });

  it("a cleared initialAgentId (plain nav) resets to the list", () => {
    const { rerender } = render(<AgentsPanel initialAgentId="a2" />);
    expect(screen.getByText("overview:a2")).toBeInTheDocument();
    rerender(<AgentsPanel initialAgentId={null} />);
    expect(screen.getByText("Select an agent to view details")).toBeInTheDocument();
  });

  it("+ New shows the chooser; hire opens the marketplace drawer, build shows the form", () => {
    mockOpenMarketplace.mockClear();
    render(<AgentsPanel />);

    fireEvent.click(screen.getByRole("button", { name: /new/i }));
    expect(screen.getByTestId("agent-create-chooser")).toBeInTheDocument();
    expect(screen.queryByText("create-form")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /hire from marketplace/i }));
    expect(mockOpenMarketplace).toHaveBeenCalledWith({ view: "browse" });
    expect(screen.queryByTestId("agent-create-chooser")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /new/i }));
    fireEvent.click(screen.getByRole("button", { name: /build your own/i }));
    expect(screen.getByText("create-form")).toBeInTheDocument();
  });
});
