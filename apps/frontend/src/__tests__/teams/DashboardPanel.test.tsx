// Smoke test for DashboardPanel — renders the four metric cards from the
// rich /dashboard payload. The full set of loading/error/populated/empty/
// normalization scenarios lives in the dedicated suite at
// __tests__/components/teams/panels/DashboardPanel.test.tsx; this file is
// kept as a thin smoke test.
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";

const mockRead = vi.fn((path: string) => {
  if (path === "/dashboard") {
    return {
      data: {
        dashboard: {
          agents: { active: 5, running: 5, paused: 0, error: 0 },
          tasks: { open: 12, inProgress: 0, blocked: 0, done: 0 },
          runActivity: [],
          costs: { monthSpendCents: 4250 },
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
  if (path === "/settings") {
    // The Paperclip company record — `name` is the H1.
    return { data: { name: "Phomo" }, isLoading: false, error: null, mutate: vi.fn() };
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
// Quick actions pull the marketplace drawer context + agent roster; this file
// asserts Home's composition, not the chips (HomeQuickActions.test.tsx does).
vi.mock("@/components/teams/dashboard/HomeQuickActions", () => ({
  HomeQuickActions: () => <section aria-label="Quick actions" />,
}));


describe("DashboardPanel", () => {
  it("renders metric cards with values from the un-flattened dashboard shape", () => {
    render(<DashboardPanel />);
    // Header — the company's own name is the h1; "Home" demotes to the overline.
    expect(
      screen.getByRole("heading", { level: 1, name: "Phomo" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Home")).toBeInTheDocument();
    expect(screen.getByLabelText("Quick actions")).toBeInTheDocument();
    // Agent telemetry is second, behind a collapsible section.
    expect(screen.getByText("Your team")).toBeInTheDocument();
    // 3 tile labels — scoped to the metrics strip ("Agents" also names the
    // ActiveAgentsPanel section heading). Spend is no longer one of them: it
    // moved to the Business section, which is mocked as loading here and
    // asserted in HomeRollupStrip.test.tsx.
    const metrics = within(screen.getByLabelText("Key metrics"));
    expect(metrics.getByText("Agents")).toBeInTheDocument();
    expect(metrics.getByText("Open Tasks")).toBeInTheDocument();
    expect(metrics.getByText("Pending Approvals")).toBeInTheDocument();
  });
});
