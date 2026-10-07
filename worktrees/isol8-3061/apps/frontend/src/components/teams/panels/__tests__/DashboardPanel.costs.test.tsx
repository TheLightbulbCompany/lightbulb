// Covers the billed-spend figure's provider-aware re-point: it renders
// straight off /teams/dashboard's optional `costs` block ({ monthSpendCents }),
// now through the Business section's Agent spend tile rather than a second
// MetricCard, and says "Not shown" — never $0 — when the backend omits `costs`
// (e.g. an owner on a provider path the meter doesn't cover yet).

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
vi.mock("@/components/teams/inbox/hooks/useAttentionFeed", () => ({
  useAttentionFeed: () => ({ items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn() }),
}));
vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>();
  return { ...mod, useLiveRuns: () => ({ runs: [], isLoading: false, isError: false }) };
});
// The Business section is the surface under test here, so its rollup is a
// bound fixture rather than the loading stub the other DashboardPanel files
// use. Two days inside the 30-day window that starts at `windowStart`.
vi.mock("@/hooks/useCompanyRollup", () => ({
  useCompanyRollup: () => ({
    rollup: {
      start: "2026-08-03",
      end: "2026-09-01",
      stripe_bound: true,
      first_day: "2026-08-30",
      days: [
        { day: "2026-08-30", revenue_microcents: 1_000_000, signups: 1, visitors: 10 },
        { day: "2026-08-31", revenue_microcents: 2_000_000, signups: 2, visitors: 20 },
      ],
      agent_spend_month_to_date_microcents: 1_400_000,
      unavailable: ["followers", "open_threads"],
    },
    windowStart: "2026-08-03",
    span: 30,
    error: undefined,
    isLoading: false,
    refresh: vi.fn(),
    isUnavailable: () => false,
  }),
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: undefined }),
}));
// Quick actions pull the marketplace drawer context + agent roster; this file
// asserts the spend tile, not the chips (HomeQuickActions.test.tsx does).
vi.mock("@/components/teams/dashboard/HomeQuickActions", () => ({
  HomeQuickActions: () => <section aria-label="Quick actions" />,
}));
// Every Business tile now calls useAskCeo() (via Tile), which in turn
// calls useAgents() — a real hook this file has no gateway context for.
vi.mock("@/hooks/useAskCeo", () => ({ useAskCeo: () => vi.fn() }));

import { DashboardPanel } from "../DashboardPanel";
import { useTeamsApi } from "@/hooks/useTeamsApi";

function makeRead(
  perPath: Record<
    string,
    { data?: unknown; isLoading?: boolean; error?: Error | null }
  >,
) {
  return vi.fn((path: string) => {
    // /artifacts is called with a query string — match on prefix so a single
    // fixture entry covers both the bare key and the param-decorated call.
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
});

describe("DashboardPanel agent spend tile", () => {
  it("renders the tile from billed cents when costs present", async () => {
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
      }),
    );
    render(<DashboardPanel />);
    expect(await screen.findByText("Agent spend")).toBeInTheDocument();
    const business = within(screen.getByLabelText("Business summary"));
    expect(business.getByText("$12.34")).toBeInTheDocument();
    expect(business.getByText("Billed this month")).toBeInTheDocument();
  });

  it("says Not shown — never $0 — when the costs block is absent", async () => {
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
      }),
    );
    render(<DashboardPanel />);
    // "Pending Approvals" no longer renders anywhere on Home (Task 5 retires
    // the agent-telemetry metric tiles it used to name); the strip mounting
    // is the settle signal now.
    await screen.findByRole("region", { name: "Business summary" });
    expect(screen.getByText("Not shown")).toBeInTheDocument();
  });
});
