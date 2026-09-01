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

/** The hook's full return shape. `isUnavailable` is consulted per tile so a
 *  column the backend names in `unavailable[]` renders as unavailable rather
 *  than as a confident 0 — a partial mock silently throws instead. */
const view = (over: Record<string, unknown>) => ({
  rollup: undefined,
  error: undefined,
  isLoading: false,
  refresh: vi.fn(),
  isUnavailable: (key: string) =>
    ((over.rollup as { unavailable?: string[] } | undefined)?.unavailable ?? []).includes(key),
  ...over,
});

beforeEach(() => {
  mockRollup.mockReset();
});

describe("HomeRollupStrip honesty", () => {
  it("says we cannot see, rather than showing zeros, when the read fails", () => {
    // A failed read is not zero revenue. Showing $0 here would be a number we
    // have not earned — the exact failure the whole state system exists for.
    mockRollup.mockReturnValue(view({ rollup: undefined, error: new Error("boom") }));
    render(<HomeRollupStrip />);

    expect(screen.getByText(/couldn't read your business numbers/i)).toBeInTheDocument();
    // `formatUsdCents(0)` renders "$0", not "$0.00" — asserting the latter is a
    // test that cannot fail, which is worse than no test on the module's
    // central promise.
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
    expect(screen.queryByText("Revenue")).not.toBeInTheDocument();
  });

  it("asks for a Stripe connection instead of reporting zero, when unbound", () => {
    // Both rollup writers iterate owners with a bound company Stripe account,
    // so an unbound owner has no rows AT ALL — including the columns that have
    // nothing to do with Stripe. Zeros here would claim knowledge we lack.
    mockRollup.mockReturnValue(
      view({ rollup: { ...LIVE, stripe_bound: false, days: [], first_day: null } }),
    );
    render(<HomeRollupStrip />);

    expect(screen.getByText(/Connect Stripe to see revenue here/i)).toBeInTheDocument();
    // `formatUsdCents(0)` renders "$0", not "$0.00" — asserting the latter is a
    // test that cannot fail, which is worse than no test on the module's
    // central promise.
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
    expect(screen.queryByText("Revenue")).not.toBeInTheDocument();
  });

  it("stays calm when unbound — an unconnected account is healthy, not an incident", () => {
    // system.md: "calm when healthy, loud only on incidents". Every new owner
    // starts here; painting it red would make a working product look broken.
    mockRollup.mockReturnValue(
      view({ rollup: { ...LIVE, stripe_bound: false, days: [], first_day: null } }),
    );
    const { container } = render(<HomeRollupStrip />);
    const notice = container.querySelector("[data-panel-notice]")!;
    expect(notice.getAttribute("data-panel-notice")).toBe("unbound");
    expect(notice.className).not.toMatch(/border-l-err/);
  });

  it("labels the range from first_day, not from the window requested", () => {
    // There is no backfill: rollups begin at the Stripe binding. Saying
    // "last 30 days" over 2 days of data under-reports without admitting it.
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip />);
    expect(screen.getByText(/Since Aug 30/)).toBeInTheDocument();
  });

  
  it("never plots agent spend as a series — it is month-to-date on every row", () => {
    // The writer stamps the running month total onto each current-month row,
    // so a sparkline would draw a staircase that reads as growth, and a sum
    // would double-count by roughly the number of days elapsed.
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip />);

    expect(screen.getByLabelText("Revenue over the period")).toBeInTheDocument();
    expect(screen.queryByLabelText("Agent spend over the period")).not.toBeInTheDocument();
    // ...and it is shown as the scalar it is.
    expect(screen.getByText("Month to date")).toBeInTheDocument();
  });
});

describe("HomeRollupStrip honesty — regressions found in review", () => {
  it("renders an unavailable column as unavailable, never as a confident 0", () => {
    // The guard that matters: when the backend names a column it cannot serve,
    // the tile must say so rather than print `sum() ?? 0` — a number nobody
    // measured. Uses `signups` because it has a tile; `visitors` deliberately
    // has none (see the component), and `followers`/`open_threads` never did.
    mockRollup.mockReturnValue(
      view({ rollup: { ...LIVE, unavailable: ["signups"] } }),
    );
    render(<HomeRollupStrip />);

    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    // ...and it must not be plotted as a flat line at zero either.
    expect(screen.queryByLabelText("Signups over the period")).not.toBeInTheDocument();
  });

  
  
  it("keeps a negative revenue day inside the sparkline's viewBox", () => {
    // `_read_day` sums balance-transaction `net` over all types, so refunds and
    // disputes can push a day below zero. A max-only scale mapped those points
    // outside viewBox="0 0 100 20", where the SVG clipped them silently and a
    // refund day rendered as a flat line rather than a drop.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          days: [
            { day: "2026-08-30", revenue_microcents: -5_000_000, signups: 0, visitors: 1 },
            { day: "2026-08-31", revenue_microcents: 1_000_000, signups: 1, visitors: 2 },
          ],
        },
      }),
    );
    const { container } = render(<HomeRollupStrip />);
    const points = container.querySelector("polyline")!.getAttribute("points")!;
    for (const pair of points.split(" ")) {
      const y = Number(pair.split(",")[1]);
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y).toBeLessThanOrEqual(20);
    }
  });
});
