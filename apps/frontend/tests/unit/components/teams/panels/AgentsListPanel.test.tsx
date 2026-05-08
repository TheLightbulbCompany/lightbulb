// apps/frontend/tests/unit/components/teams/panels/AgentsListPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + PanelToolbar +
// PanelDataTable + PanelEmptyState + EditorialDialog composition on the
// Agents list panel. Behavior tests (whitelisted POST body — security-critical
// guard against adapter-field smuggling, link href) live in
// src/__tests__/teams/AgentsListPanel.test.tsx.

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

import { AgentsListPanel } from "@/components/teams/panels/AgentsListPanel";

describe("AgentsListPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
  });

  it("renders PanelMasthead with overline + h1 title + New agent CTA", () => {
    mockRead.mockReturnValue({
      data: { agents: [] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });

    render(<AgentsListPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /agents/i }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /new agent/i }).length,
    ).toBeGreaterThan(0);
  });

  it("renders the editorial PanelToolbar search input (role=searchbox)", () => {
    mockRead.mockReturnValue({
      data: { agents: [] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });

    render(<AgentsListPanel />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("renders PanelEmptyState shape (h2 with editorial-serif class) when there are no agents", () => {
    mockRead.mockReturnValue({
      data: { agents: [] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });

    render(<AgentsListPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no agents yet/i);
  });
});
