// apps/frontend/tests/unit/components/teams/panels/AgentDetailPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + PanelTabs
// composition on the Agent detail panel. Behavior tests (overview/runs/config
// tab content + the operator-controlled config disclaimer) live in
// src/__tests__/teams/AgentDetailPanel.test.tsx.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const mockRead = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

import { AgentDetailPanel } from "@/components/teams/panels/AgentDetailPanel";

describe("AgentDetailPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
    mockRead.mockImplementation((path: string) => {
      if (path === "/agents/a1")
        return {
          data: { id: "a1", name: "Goose", role: "engineer" },
          isLoading: false,
          error: null,
          mutate: vi.fn(),
        };
      if (path === "/agents/a1/runs")
        return {
          data: { runs: [] },
          isLoading: false,
          error: null,
          mutate: vi.fn(),
        };
      return { data: undefined, isLoading: false, error: null };
    });
  });

  it("renders PanelMasthead with overline + h1 title (agent name)", () => {
    render(<AgentDetailPanel agentId="a1" />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /goose/i }),
    ).toBeInTheDocument();
  });

  it("renders PanelTabs with overview/runs/config", () => {
    render(<AgentDetailPanel agentId="a1" />);
    expect(screen.getByRole("tab", { name: /overview/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /runs/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /config/i })).toBeInTheDocument();
  });
});
