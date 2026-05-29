// Test for AgentDetailPanel.
//
// The panel now has Manage + Runs tabs (the old Overview JSON dump + disabled
// Config tab were replaced by AgentManageSection). Meta (role/title/etc.) is
// read-only here — managed in the /chat control panel (single-editor rule).
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => {
      if (path.endsWith("/runs"))
        return {
          data: { runs: [{ id: "r1", status: "ok", startedAt: "" }] },
          isLoading: false,
          error: null,
          mutate: vi.fn(),
        };
      if (path === "/agents")
        return { data: { agents: [] }, isLoading: false, error: null, mutate: vi.fn() };
      return {
        data: { id: "a1", name: "Alice", role: "ceo" },
        isLoading: false,
        error: null,
        mutate: vi.fn(),
      };
    },
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

// AgentManageSection (rendered on the default Manage tab) uses useAgentMutations,
// which reaches the gateway — mock it so the panel renders without a provider.
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({
    setReportsTo: vi.fn(),
    pause: vi.fn(),
    resume: vi.fn(),
    remove: vi.fn(),
  }),
}));

import { AgentDetailPanel } from "@/components/teams/panels/AgentDetailPanel";

describe("AgentDetailPanel", () => {
  it("renders agent name and role", () => {
    render(<AgentDetailPanel agentId="a1" />);
    expect(screen.getByText("Alice")).toBeInTheDocument();
    // PanelMasthead renders the role as meta in uppercase.
    expect(screen.getByText("CEO")).toBeInTheDocument();
  });

  it("switches to Runs tab and shows run rows", async () => {
    const user = userEvent.setup();
    render(<AgentDetailPanel agentId="a1" />);
    await user.click(screen.getByRole("tab", { name: /runs/i }));
    expect(screen.getByText("ok")).toBeInTheDocument();
    const link = screen.getByText("Open →");
    expect(link.closest("a")).toHaveAttribute(
      "href",
      "/teams/agents/a1/runs/r1",
    );
  });

  it("Manage tab shows read-only meta managed in the control panel (no edit form)", () => {
    render(<AgentDetailPanel agentId="a1" />);
    // Default tab is Manage; meta fields are read-only with a pointer to the
    // control panel — never an in-panel edit form for role/title/etc.
    expect(
      screen.getByText(/managed in the agent.s control panel/i),
    ).toBeInTheDocument();
  });
});
