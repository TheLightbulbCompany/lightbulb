import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { UsagePanel } from "../UsagePanel";

// Covers the "Subscription: <status>" line rendered above the usage
// sections. Before this fix it rendered `account.subscription_status` raw
// through a Tailwind `capitalize` class — a trial_local owner saw
// "Subscription: Trial_local". Must now go through planLabel(), with
// `capitalize` dropped for trial_local so "Free tier" doesn't come out
// "Free Tier".
//
// Also covers the `expired` flag: UsagePanel can be docked as a sibling of
// WorkspaceSidebar in the desktop dockable shell (WorkspaceShell.tsx), so an
// expired free-tier owner must see "Free tier ended" here too, not just in
// the sidebar footer — otherwise the two badges contradict each other on the
// same screen.

let billingAccount: { subscription_status: string | null; trial_end?: number | null } | null =
  null;
vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: billingAccount,
    isLoading: false,
    fetchUsage: async () => null,
    refresh: () => {},
  }),
}));
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

  it("capitalizes other statuses via planLabel, with the capitalize class intact", () => {
    billingAccount = { subscription_status: "active" };
    render(<UsagePanel />);
    const label = screen.getByText("Active");
    expect(label.className).toContain("capitalize");
  });

  it("reads Free tier ended once the free-tier window has elapsed", () => {
    billingAccount = {
      subscription_status: "trial_local",
      trial_end: Math.floor(Date.now() / 1000) - 86_400, // 1 day ago — expired
    };
    render(<UsagePanel />);
    expect(screen.getByText("Free tier ended")).toBeInTheDocument();
    expect(screen.queryByText("Free tier")).not.toBeInTheDocument();
  });

  it("stays Free tier while the free-tier window is still open", () => {
    billingAccount = {
      subscription_status: "trial_local",
      trial_end: Math.floor(Date.now() / 1000) + 86_400, // 1 day out — not expired
    };
    render(<UsagePanel />);
    expect(screen.getByText("Free tier")).toBeInTheDocument();
    expect(screen.queryByText("Free tier ended")).not.toBeInTheDocument();
  });
});
