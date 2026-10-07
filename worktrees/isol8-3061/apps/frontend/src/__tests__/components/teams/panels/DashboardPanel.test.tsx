import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("@/hooks/useCompanyRollup", () => ({
  useCompanyRollup: () => ({
    rollup: undefined, windowStart: "2026-08-03", span: 30, error: undefined, isLoading: true,
    refresh: vi.fn(), isUnavailable: () => false,
  }),
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: undefined }),
}));
vi.mock("@/components/teams/dashboard/HomeQuickActions", () => ({
  HomeQuickActions: () => <section aria-label="Quick actions" />,
}));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: vi.fn() }));
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

import { DashboardPanel } from "@/components/teams/panels/DashboardPanel";
import { useTeamsApi } from "@/hooks/useTeamsApi";

function mockRead(perPath: Record<string, { data?: unknown; isLoading?: boolean; error?: Error | null }>) {
  const read = vi.fn((path: string) => {
    const key = Object.keys(perPath).find((k) => path === k || path.startsWith(`${k}?`));
    const r = key ? perPath[key] : {};
    return { data: r.data, isLoading: r.isLoading ?? false, error: r.error ?? null, mutate: vi.fn(), isValidating: false };
  });
  vi.mocked(useTeamsApi).mockReturnValue({ read } as unknown as ReturnType<typeof useTeamsApi>);
  return read;
}

describe("DashboardPanel — Home is the dashboard", () => {
  beforeEach(() => vi.clearAllMocks());

  it("puts the company name in the H1 and the three detail sections under the business strip", () => {
    mockRead({
      "/settings": { data: { name: "Northwind Candle Co." } },
      "/dashboard": { data: { dashboard: { agents: {}, tasks: { open: 12 }, runActivity: [], pendingApprovals: 0, budgets: { pendingApprovals: 0 } } } },
      "/issues": { data: [] },
      "/artifacts": { data: { artifacts: [], nextCursor: null } },
    });
    render(<DashboardPanel />);
    expect(screen.getByRole("heading", { level: 1, name: "Northwind Candle Co." })).toBeInTheDocument();
    const strip = screen.getByRole("region", { name: "Business summary" });
    const needs = screen.getByRole("region", { name: "Needs you" });
    const now = screen.getByRole("region", { name: "Happening now" });
    const work = screen.getByRole("region", { name: "Recent work" });
    // Order: strip → quick actions → sections.
    const before = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(before(strip, screen.getByLabelText("Quick actions"))).toBe(true);
    expect(before(screen.getByLabelText("Quick actions"), needs)).toBe(true);
    expect(before(needs, now)).toBe(true);
    expect(before(now, work)).toBe(true);
    expect(screen.getByRole("link", { name: "Open Tasks · 12 open" })).toBeInTheDocument();
  });

  it("no longer renders the agent telemetry", () => {
    mockRead({ "/settings": { data: { name: "Phomo" } }, "/dashboard": { data: { dashboard: { agents: { running: 2 }, tasks: { open: 3 }, runActivity: [], pendingApprovals: 1, budgets: { pendingApprovals: 0 } } } } });
    render(<DashboardPanel />);
    for (const gone of ["Your team", "Key metrics", "Run Activity", "Success Rate", "Recent Tasks", "Recent activity", "Pending Approvals"]) {
      expect(screen.queryByText(gone)).not.toBeInTheDocument();
      expect(screen.queryByLabelText(gone)).not.toBeInTheDocument();
    }
  });

  it("does not print an open-task count while /dashboard is still loading", () => {
    mockRead({ "/settings": { data: {} }, "/dashboard": { isLoading: true } });
    render(<DashboardPanel />);
    expect(screen.getByRole("heading", { level: 1, name: "Home" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Tasks" })).toBeInTheDocument();
    expect(screen.queryByText(/open$/)).not.toBeInTheDocument();
  });

  it("does not print an open-task count when /dashboard fails outright — a failed read is not a zero", () => {
    mockRead({
      "/settings": { data: { name: "Phomo" } },
      "/dashboard": { error: new Error("boom") },
    });
    render(<DashboardPanel />);
    expect(screen.getByRole("link", { name: "Open Tasks" })).toBeInTheDocument();
    expect(screen.queryByText(/0 open/)).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Happening now" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Recent work" })).toBeInTheDocument();
  });
});
