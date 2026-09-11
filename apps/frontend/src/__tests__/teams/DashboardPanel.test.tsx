// Smoke test for DashboardPanel — the company name in the H1, quick actions,
// and the three Home sections (Needs you / Happening now / Recent work),
// with the retired agent-telemetry tiles confirmed gone. The full set of
// loading/error/populated/empty/normalization scenarios lives in the
// dedicated suite at __tests__/components/teams/panels/DashboardPanel.test.tsx;
// this file is kept as a thin smoke test.
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const mockRead = vi.fn((path: string) => {
  if (path === "/dashboard") {
    // Only `tasks.open` and `costs` are read by DashboardPanel/HomeRollupStrip;
    // everything else on DashboardSummary is dead weight in this fixture.
    return {
      data: { dashboard: { tasks: { open: 12 }, costs: { monthSpendCents: 4250 } } },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    };
  }
  if (path === "/settings") {
    // The Paperclip company record — `name` is the H1.
    return { data: { name: "Phomo" }, isLoading: false, error: null, mutate: vi.fn() };
  }
  // /issues, etc.
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
vi.mock("@/components/teams/inbox/hooks/useAttentionFeed", () => ({
  useAttentionFeed: () => ({ items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn() }),
}));
vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>();
  return { ...mod, useLiveRuns: () => ({ runs: [], isLoading: false, isError: false }) };
});
// Every Business tile now calls useAskCeo() (via Tile), which in turn
// calls useAgents() — a real hook this file has no gateway context for.
vi.mock("@/hooks/useAskCeo", () => ({ useAskCeo: () => vi.fn() }));


describe("DashboardPanel", () => {
  it("renders the company name, the quick actions and the three sections", () => {
    render(<DashboardPanel />);
    expect(screen.getByRole("heading", { level: 1, name: "Phomo" })).toBeInTheDocument();
    expect(screen.getByText("Home")).toBeInTheDocument();
    expect(screen.getByLabelText("Quick actions")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Happening now" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recent work" })).toBeInTheDocument();
    expect(screen.queryByText("Your team")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Key metrics")).not.toBeInTheDocument();
  });
});
