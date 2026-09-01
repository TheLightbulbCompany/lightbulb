// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { HomeRollupStrip } from "../HomeRollupStrip";

const mockRollup = vi.fn();
vi.mock("@/hooks/useCompanyRollup", () => ({
  useCompanyRollup: () => mockRollup(),
}));

const LIVE = {
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
};

beforeEach(() => {
  mockRollup.mockReset();
});

describe("HomeRollupStrip honesty", () => {
  it("says we cannot see, rather than showing zeros, when the read fails", () => {
    // A failed read is not zero revenue. Showing $0 here would be a number we
    // have not earned — the exact failure the whole state system exists for.
    mockRollup.mockReturnValue({ rollup: undefined, error: new Error("boom"), isLoading: false });
    render(<HomeRollupStrip />);

    expect(screen.getByText(/couldn't read your business numbers/i)).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("asks for a Stripe connection instead of reporting zero, when unbound", () => {
    // Both rollup writers iterate owners with a bound company Stripe account,
    // so an unbound owner has no rows AT ALL — including the columns that have
    // nothing to do with Stripe. Zeros here would claim knowledge we lack.
    mockRollup.mockReturnValue({
      rollup: { ...LIVE, stripe_bound: false, days: [], first_day: null },
      error: undefined,
      isLoading: false,
    });
    render(<HomeRollupStrip />);

    expect(screen.getByText(/Connect Stripe to see revenue here/i)).toBeInTheDocument();
    expect(screen.queryByText("$0.00")).not.toBeInTheDocument();
  });

  it("stays calm when unbound — an unconnected account is healthy, not an incident", () => {
    // system.md: "calm when healthy, loud only on incidents". Every new owner
    // starts here; painting it red would make a working product look broken.
    mockRollup.mockReturnValue({
      rollup: { ...LIVE, stripe_bound: false, days: [], first_day: null },
      error: undefined,
      isLoading: false,
    });
    const { container } = render(<HomeRollupStrip />);
    const notice = container.querySelector("[data-panel-notice]")!;
    expect(notice.getAttribute("data-panel-notice")).toBe("unbound");
    expect(notice.className).not.toMatch(/border-l-err/);
  });

  it("labels the range from first_day, not from the window requested", () => {
    // There is no backfill: rollups begin at the Stripe binding. Saying
    // "last 30 days" over 2 days of data under-reports without admitting it.
    mockRollup.mockReturnValue({ rollup: LIVE, error: undefined, isLoading: false });
    render(<HomeRollupStrip />);
    expect(screen.getByText(/Since Aug 30/)).toBeInTheDocument();
  });

  it("names the columns it cannot serve instead of showing them as zero", () => {
    mockRollup.mockReturnValue({ rollup: LIVE, error: undefined, isLoading: false });
    render(<HomeRollupStrip />);
    expect(screen.getByText(/Followers and open threads aren't wired up yet/i)).toBeInTheDocument();
  });

  it("never plots agent spend as a series — it is month-to-date on every row", () => {
    // The writer stamps the running month total onto each current-month row,
    // so a sparkline would draw a staircase that reads as growth, and a sum
    // would double-count by roughly the number of days elapsed.
    mockRollup.mockReturnValue({ rollup: LIVE, error: undefined, isLoading: false });
    render(<HomeRollupStrip />);

    expect(screen.getByLabelText("Revenue over the period")).toBeInTheDocument();
    expect(screen.queryByLabelText("Agent spend over the period")).not.toBeInTheDocument();
    // ...and it is shown as the scalar it is.
    expect(screen.getByText("Month to date")).toBeInTheDocument();
  });
});
