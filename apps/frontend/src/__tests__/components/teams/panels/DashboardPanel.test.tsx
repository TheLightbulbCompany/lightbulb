// Tests for DashboardPanel: loading / error / metric tiles / chart cards /
// empty + populated activity-feed states. Mocks the BFF reader and
// useActivityFeed so the panel exercises only its own composition logic.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("@/components/teams/dashboard/hooks/useActivityFeed", () => ({
  useActivityFeed: vi.fn(),
}));

import { DashboardPanel } from "@/components/teams/panels/DashboardPanel";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";

function makeRead(
  perPath: Record<
    string,
    { data?: unknown; isLoading?: boolean; error?: Error | null }
  >,
) {
  return vi.fn((path: string) => {
    const r = perPath[path] ?? { data: undefined, isLoading: false, error: null };
    return {
      data: r.data,
      isLoading: r.isLoading ?? false,
      error: r.error ?? null,
      mutate: vi.fn(),
      isValidating: false,
    };
  });
}

function mockApi(read: ReturnType<typeof makeRead>) {
  vi.mocked(useTeamsApi).mockReturnValue({
    read,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  } as unknown as ReturnType<typeof useTeamsApi>);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(useActivityFeed).mockReturnValue({
    events: [],
    isLoading: false,
    isError: false,
  });
});

describe("DashboardPanel", () => {
  test("renders Loading state when dashboard not yet loaded", () => {
    mockApi(makeRead({ "/dashboard": { isLoading: true } }));
    render(<DashboardPanel />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  test("renders error state on dashboard fetch failure", () => {
    mockApi(makeRead({ "/dashboard": { error: new Error("boom") } }));
    render(<DashboardPanel />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  test("renders 4 metric tiles when dashboard loaded", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: { active: 1, running: 2, paused: 0, error: 0 },
              tasks: { open: 5, inProgress: 0, blocked: 1, done: 3 },
              runActivity: [],
              costs: {
                monthSpendCents: 1234,
                monthBudgetCents: 0,
                monthUtilizationPercent: 0,
              },
              pendingApprovals: 2,
              budgets: { pendingApprovals: 1 },
            },
          },
        },
        "/issues": { data: [] },
      }),
    );
    render(<DashboardPanel />);
    expect(screen.getByText("Agents")).toBeInTheDocument();
    expect(screen.getByText("Open Tasks")).toBeInTheDocument();
    expect(screen.getByText("Month Spend")).toBeInTheDocument();
    expect(screen.getByText("Pending Approvals")).toBeInTheDocument();
  });

  test("renders 4 chart cards when dashboard loaded", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              costs: {
                monthSpendCents: 0,
                monthBudgetCents: 0,
                monthUtilizationPercent: 0,
              },
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        "/issues": { data: [] },
      }),
    );
    render(<DashboardPanel />);
    expect(screen.getByText("Run Activity")).toBeInTheDocument();
    expect(screen.getByText("Issues by Priority")).toBeInTheDocument();
    expect(screen.getByText("Issues by Status")).toBeInTheDocument();
    expect(screen.getByText("Success Rate")).toBeInTheDocument();
  });

  test("renders empty activity message when no events", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              costs: {
                monthSpendCents: 0,
                monthBudgetCents: 0,
                monthUtilizationPercent: 0,
              },
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        "/issues": { data: [] },
      }),
    );
    render(<DashboardPanel />);
    expect(screen.getByText(/no recent activity/i)).toBeInTheDocument();
  });

  test("renders activity events when feed populated", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              costs: {
                monthSpendCents: 0,
                monthBudgetCents: 0,
                monthUtilizationPercent: 0,
              },
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        "/issues": { data: [] },
      }),
    );
    vi.mocked(useActivityFeed).mockReturnValue({
      events: [
        {
          id: "evt_1",
          action: "issue.created",
          createdAt: new Date().toISOString(),
        },
      ],
      isLoading: false,
      isError: false,
    });
    render(<DashboardPanel />);
    expect(screen.queryByText(/no recent activity/i)).not.toBeInTheDocument();
  });

  test("formats month spend as currency", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              costs: {
                monthSpendCents: 12345,
                monthBudgetCents: 50000,
                monthUtilizationPercent: 25,
              },
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        "/issues": { data: [] },
      }),
    );
    render(<DashboardPanel />);
    expect(screen.getByText("$123.45")).toBeInTheDocument();
    // utilization summary in description block
    expect(
      screen.getByText(/25% of \$500\.00 budget/i),
    ).toBeInTheDocument();
  });

  test("normalizes /issues envelope { items: [...] }", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              costs: {
                monthSpendCents: 0,
                monthBudgetCents: 0,
                monthUtilizationPercent: 0,
              },
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        "/issues": {
          data: {
            items: [
              {
                id: "i1",
                title: "x",
                status: "todo",
                priority: "high",
                createdAt: "2026-05-05T00:00:00Z",
              },
            ],
          },
        },
      }),
    );
    // No throw + chart cards still present is sufficient signal that the
    // envelope-normalization branch is exercised.
    render(<DashboardPanel />);
    expect(screen.getByText("Issues by Priority")).toBeInTheDocument();
  });
});
