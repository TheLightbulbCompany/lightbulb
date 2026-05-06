// Smoke test for DashboardPanel — renders the four metric cards from the
// rich /dashboard payload (un-flattened in PR #2 task 1). The full set of
// loading/error/populated/empty/normalization scenarios lives in the
// dedicated suite at __tests__/components/teams/panels/DashboardPanel.test.tsx;
// this file is kept as a thin smoke test from the original Inbox PR.
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const mockRead = vi.fn((path: string) => {
  if (path === "/dashboard") {
    return {
      data: {
        dashboard: {
          agents: { active: 5, running: 5, paused: 0, error: 0 },
          tasks: { open: 12, inProgress: 0, blocked: 0, done: 0 },
          runActivity: [],
          costs: {
            monthSpendCents: 4250,
            monthBudgetCents: 0,
            monthUtilizationPercent: 0,
          },
          pendingApprovals: 0,
          budgets: { pendingApprovals: 0 },
        },
        sidebar_badges: { inbox: 2 },
      },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    };
  }
  // /issues + /activity etc.
  return {
    data: [],
    isLoading: false,
    error: null,
    mutate: vi.fn(),
  };
});

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

import { DashboardPanel } from "@/components/teams/panels/DashboardPanel";

describe("DashboardPanel", () => {
  it("renders metric cards with values from the un-flattened dashboard shape", () => {
    render(<DashboardPanel />);
    // Header
    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.getByText("Dashboard")).toBeInTheDocument();
    // 4 tile labels
    expect(screen.getByText("Agents")).toBeInTheDocument();
    expect(screen.getByText("Open Tasks")).toBeInTheDocument();
    expect(screen.getByText("Month Spend")).toBeInTheDocument();
    expect(screen.getByText("Pending Approvals")).toBeInTheDocument();
    // 4250 cents → $42.50 (unique enough to avoid description-text collisions)
    expect(screen.getByText("$42.50")).toBeInTheDocument();
  });
});
