import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UsagePanel } from "../UsagePanel";

const usageFixture = {
  period: "2026-07",
  total_spend: 12.3456,
  total_input_tokens: 1000,
  total_output_tokens: 2000,
  total_cache_read_tokens: 0,
  total_cache_write_tokens: 0,
  request_count: 5,
  lifetime_spend: 99,
  by_member: [
    {
      user_id: "u1",
      display_name: "Ada",
      email: null,
      total_spend: 5,
      total_input_tokens: 1,
      total_output_tokens: 1,
      request_count: 2,
    },
  ],
};

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: { subscription_status: "active" },
    isLoading: false,
    fetchUsage: async () => usageFixture,
    refresh: () => {},
  }),
}));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: null }) }));

// UsagePanel's own useSWR("/billing/usage", fetchUsage) call needs the
// fixture back synchronously (matches the convention used by sibling panel
// tests, e.g. OverviewPanel.test.tsx, which mock swr directly rather than
// mocking useBilling's SWR cache key).
vi.mock("swr", () => ({
  default: (key: string) => {
    if (key === "/billing/usage") {
      return { data: usageFixture, error: undefined, isLoading: false, mutate: vi.fn() };
    }
    return { data: undefined, error: undefined, isLoading: false, mutate: vi.fn() };
  },
}));

describe("UsagePanel spend display", () => {
  it("never renders the usage-counters dollar figures", async () => {
    render(<UsagePanel />);
    expect(await screen.findByText(/5 requests/)).toBeInTheDocument();
    expect(screen.queryByText(/Total spend/)).toBeNull();
    expect(screen.queryByText(/Lifetime/)).toBeNull();
    expect(screen.queryByText("$12.3456")).toBeNull();
    // member table keeps requests, drops the Spend column
    expect(screen.queryByText("Spend")).toBeNull();
  });
});
