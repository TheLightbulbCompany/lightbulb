// Test for AgentDetailPanel.
//
// Confirms the overview/runs/config tab switching works and that the config
// tab renders the operator-controlled disclaimer (no edit form ever).
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

  it("Config tab shows operator-controlled disclaimer (no edit form)", async () => {
    const user = userEvent.setup();
    render(<AgentDetailPanel agentId="a1" />);
    await user.click(screen.getByRole("tab", { name: /config/i }));
    expect(
      screen.getByText(/Adapter configuration is managed by Isol8/i),
    ).toBeInTheDocument();
  });
});
