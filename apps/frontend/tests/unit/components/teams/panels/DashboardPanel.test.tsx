// apps/frontend/tests/unit/components/teams/panels/DashboardPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead at the top of
// the Dashboard panel. The KPI tiles, charts, and activity feed sub-trees
// are out of scope here — they have their own components and aren't part of
// the atom port. See plan 2026-05-07-teams-dashboard-approvals-atoms-port.md.

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

vi.mock("@/components/teams/dashboard/hooks/useActivityFeed", () => ({
  useActivityFeed: () => ({ events: [], isLoading: false }),
}));

import { DashboardPanel } from "@/components/teams/panels/DashboardPanel";

describe("DashboardPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
    mockRead.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });
  });

  it("renders PanelMasthead with overline + h1 title + meta", () => {
    render(<DashboardPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /dashboard/i }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("panel-masthead-meta")).toHaveTextContent(
      /last 14 days/i,
    );
  });

  it("renders the editorial-page wrapper + masthead even when data is empty", () => {
    render(<DashboardPanel />);
    const heading = screen.getByRole("heading", { level: 1, name: /dashboard/i });
    expect(heading).toHaveClass("font-editorial-serif");
  });
});
