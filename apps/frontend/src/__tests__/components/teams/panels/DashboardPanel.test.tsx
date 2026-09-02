// Tests for DashboardPanel: loading / error / metric tiles / chart cards /
// empty + populated activity-feed states. Mocks the BFF reader and
// useActivityFeed so the panel exercises only its own composition logic.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

// HomeRollupStrip mounts `useCompanyRollup`, a real SWR hook. Left unmocked it
// keeps a revalidation promise alive past this file's teardown and throws
// `document is not defined` from swr's isVisible() — which surfaces as an
// unhandled error and a non-zero exit even with every test passing. These
// files assert the agent-metrics dashboard, not the business strip; that has
// its own test file.
vi.mock("@/hooks/useCompanyRollup", () => ({
  useCompanyRollup: () => ({
    rollup: undefined,
    error: undefined,
    isLoading: true,
    refresh: vi.fn(),
    isUnavailable: () => false,
  }),
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: undefined }),
}));
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

  test("renders the three agent metric tiles when dashboard loaded", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: { active: 1, running: 2, paused: 0, error: 0 },
              tasks: { open: 5, inProgress: 0, blocked: 1, done: 3 },
              runActivity: [],
              costs: { monthSpendCents: 1234 },
              pendingApprovals: 2,
              budgets: { pendingApprovals: 1 },
            },
          },
        },
        "/issues": { data: [] },
      }),
    );
    render(<DashboardPanel />);
    // Scoped to the metrics strip — "Agents" also names the ActiveAgentsPanel
    // section heading.
    const metrics = within(screen.getByLabelText("Key metrics"));
    expect(metrics.getByText("Agents")).toBeInTheDocument();
    expect(metrics.getByText("Open Tasks")).toBeInTheDocument();
    expect(metrics.getByText("Pending Approvals")).toBeInTheDocument();
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
              costs: { monthSpendCents: 0 },
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
              costs: { monthSpendCents: 0 },
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
              costs: { monthSpendCents: 0 },
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

  test("renders Recent Tasks sorted by updatedAt desc, newest first", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              costs: { monthSpendCents: 0 },
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        // Intentionally out of order — the panel must sort by updatedAt desc.
        "/issues": {
          data: [
            {
              id: "i_old",
              identifier: "ISO-1",
              title: "Oldest task",
              status: "todo",
              updatedAt: "2026-01-01T00:00:00Z",
            },
            {
              id: "i_new",
              identifier: "ISO-3",
              title: "Newest task",
              status: "in_progress",
              updatedAt: "2026-03-01T00:00:00Z",
            },
            {
              id: "i_mid",
              identifier: "ISO-2",
              title: "Middle task",
              status: "done",
              updatedAt: "2026-02-01T00:00:00Z",
            },
          ],
        },
      }),
    );
    render(<DashboardPanel />);

    const section = screen.getByRole("region", { name: /recent tasks/i });
    expect(section).toBeInTheDocument();
    expect(screen.getByText("Recent Tasks")).toBeInTheDocument();

    // All three titles present, and ordered newest → oldest in the DOM.
    const titles = ["Newest task", "Middle task", "Oldest task"].map(
      (t) => screen.getByText(t),
    );
    titles.forEach((el) => expect(el).toBeInTheDocument());
    const positions = titles.map((el) =>
      // compareDocumentPosition: bit 4 = el2 follows el1.
      titles[0].compareDocumentPosition(el),
    );
    // Middle + Oldest follow Newest in document order.
    expect(positions[1] & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(positions[2] & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    // Identifier chip is rendered for each row.
    expect(screen.getByText("ISO-3")).toBeInTheDocument();
  });

  test("renders the Recent Tasks empty state when no issues", () => {
    mockApi(
      makeRead({
        "/dashboard": {
          data: {
            dashboard: {
              agents: {},
              tasks: {},
              runActivity: [],
              costs: { monthSpendCents: 0 },
              pendingApprovals: 0,
              budgets: { pendingApprovals: 0 },
            },
          },
        },
        "/issues": { data: [] },
      }),
    );
    render(<DashboardPanel />);
    expect(screen.getByText(/no recent tasks/i)).toBeInTheDocument();
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
              costs: { monthSpendCents: 0 },
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
