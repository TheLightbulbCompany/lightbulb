// Covers the Month Spend card's provider-aware re-point (Task 8): the card
// now renders straight off /teams/dashboard's optional `costs` block
// ({ monthSpendCents }) rather than the removed budget/utilization fields,
// and disappears entirely when the backend omits `costs` (e.g. an owner on
// a provider path the meter doesn't cover yet).

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("@/components/teams/dashboard/hooks/useActivityFeed", () => ({
  useActivityFeed: vi.fn(),
}));
vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", () => ({
  useLiveRuns: vi.fn(),
}));

import { DashboardPanel } from "../DashboardPanel";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useActivityFeed } from "@/components/teams/dashboard/hooks/useActivityFeed";
import { useLiveRuns } from "@/components/teams/dashboard/hooks/useLiveRuns";

function makeRead(
  perPath: Record<
    string,
    { data?: unknown; isLoading?: boolean; error?: Error | null }
  >,
) {
  return vi.fn((path: string) => {
    // /activity and /inbox/live-runs are called with query strings — match on
    // prefix so a single fixture entry covers both the bare key and the
    // param-decorated call.
    const key = Object.keys(perPath).find((k) => path === k || path.startsWith(`${k}?`));
    const r = key ? perPath[key] : { data: undefined, isLoading: false, error: null };
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
  vi.mocked(useLiveRuns).mockReturnValue({
    runs: [],
    isLoading: false,
    isError: false,
  });
});

describe("DashboardPanel month spend card", () => {
  it("renders the card from billed cents when costs present", async () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              costs: { monthSpendCents: 1234 },
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        "/issues": { data: [] },
        "/agents": { data: { agents: [] } },
      }),
    );
    render(<DashboardPanel />);
    expect(await screen.findByText("Month Spend")).toBeInTheDocument();
    const metrics = within(screen.getByLabelText("Key metrics"));
    expect(metrics.getByText("$12.34")).toBeInTheDocument();
    expect(metrics.getByText(/Billed credits this month/i)).toBeInTheDocument();
  });

  it("hides the card when costs block absent", async () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        "/issues": { data: [] },
        "/agents": { data: { agents: [] } },
      }),
    );
    render(<DashboardPanel />);
    expect(await screen.findByText(/Pending Approvals/)).toBeInTheDocument();
    expect(screen.queryByText("Month Spend")).toBeNull();
  });
});
