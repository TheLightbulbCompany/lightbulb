import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { UsagePanel } from "../UsagePanel";

// Covers the "Subscription: <status>" line rendered above the usage
// sections. Before this fix it rendered `account.subscription_status` raw
// through a Tailwind `capitalize` class — a trial_local owner saw
// "Subscription: Trial_local". Must now go through planLabel(), with
// `capitalize` dropped for trial_local so "Free tier" doesn't come out
// "Free Tier".

let billingAccount: { subscription_status: string | null } | null = null;
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billingAccount,
    isLoading: false,
    fetchUsage: async () => null,
    refresh: () => {},
  }),
}));
vi.mock("@/hooks/useSpend", () => ({ useSpend: () => ({ spend: undefined, isLoading: false }) }));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: null }) }));
vi.mock("swr", () => ({
  default: () => ({ data: undefined, error: undefined, isLoading: false, mutate: vi.fn() }),
}));

describe("UsagePanel subscription badge", () => {
  beforeEach(() => {
    billingAccount = null;
  });

  it("renders nothing when there is no billing account yet", () => {
    render(<UsagePanel />);
    expect(screen.queryByText(/Subscription:/)).toBeNull();
  });

  it("maps trial_local to Free tier without the capitalize transform mangling it", () => {
    billingAccount = { subscription_status: "trial_local" };
    render(<UsagePanel />);
    expect(screen.getByText(/Subscription:/)).toBeInTheDocument();
    const label = screen.getByText("Free tier");
    expect(label.className).not.toContain("capitalize");
    expect(screen.queryByText(/trial_local/i)).not.toBeInTheDocument();
  });

  it("passes other statuses through with the capitalize class intact", () => {
    billingAccount = { subscription_status: "active" };
    render(<UsagePanel />);
    const label = screen.getByText("active");
    expect(label.className).toContain("capitalize");
  });
});
