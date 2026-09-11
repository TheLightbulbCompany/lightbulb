// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

import { HomeRollupStrip } from "../HomeRollupStrip";

// The span argument is FORWARDED, not discarded: the toggle's only job is to
// change what the hook is asked for, and a mock that swallows the argument
// cannot fail when that wiring breaks.
const mockRollup = vi.fn();
vi.mock("@/hooks/useCompanyRollup", () => ({
  useCompanyRollup: (span: number) => mockRollup(span),
}));

// Every tile's "Ask CEO" hands off to the same hook — a plain spy is
// enough to assert the exact draft each tile sends.
const askCeo = vi.fn();
vi.mock("@/hooks/useAskCeo", () => ({ useAskCeo: () => askCeo }));

// Only `details_submitted` is read, and only when there are no rows at all —
// a bound-but-unonboarded account returns legitimately empty, and "finish
// setup" is a different instruction from "nothing happened yet".
const mockStatus = vi.fn(() => undefined);
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  useCompanyStripeStatus: () => ({ data: mockStatus() }),
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
  windowStart: "2026-08-03",
  span: 30,
  error: undefined,
  isLoading: false,
  refresh: vi.fn(),
  isUnavailable: (key: string) =>
    ((over.rollup as { unavailable?: string[] } | undefined)?.unavailable ?? []).includes(key),
  ...over,
});

beforeEach(() => {
  mockRollup.mockReset();
  mockStatus.mockReset();
});

describe("HomeRollupStrip honesty", () => {
  it("says we cannot see, rather than showing zeros, when the read fails", () => {
    // A failed read is not zero revenue. Showing $0 here would be a number we
    // have not earned — the exact failure the whole state system exists for.
    mockRollup.mockReturnValue(view({ rollup: undefined, error: new Error("boom") }));
    render(<HomeRollupStrip spendCents={1400} />);

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
    render(<HomeRollupStrip spendCents={1400} />);

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
    const { container } = render(<HomeRollupStrip spendCents={1400} />);
    const notice = container.querySelector("[data-panel-notice]")!;
    expect(notice.getAttribute("data-panel-notice")).toBe("unbound");
    expect(notice.className).not.toMatch(/border-l-err/);
  });

  it("labels the range from first_day, not from the window requested", () => {
    // There is no backfill: rollups begin at the Stripe binding. Saying
    // "last 30 days" over 2 days of data under-reports without admitting it.
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getByText(/Since Aug 30/)).toBeInTheDocument();
  });


  it("never plots agent spend as a series — it is month-to-date on every row", () => {
    // The writer stamps the running month total onto each current-month row,
    // so a sparkline would draw a staircase that reads as growth, and a sum
    // would double-count by roughly the number of days elapsed.
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip spendCents={1400} />);

    expect(screen.getByLabelText("Revenue over the period")).toBeInTheDocument();
    expect(screen.queryByLabelText("Agent spend over the period")).not.toBeInTheDocument();
    // ...and it is shown as the scalar it is — the Metronome-billed figure the
    // Spend tab prints, passed in, not the rollup's own month-to-date column.
    expect(screen.getByText("Billed this month")).toBeInTheDocument();
    expect(screen.getByText("$14")).toBeInTheDocument();
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
    render(<HomeRollupStrip spendCents={1400} />);

    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    // ...and it must not be plotted as a flat line at zero either.
    expect(screen.queryByLabelText("New customers over the period")).not.toBeInTheDocument();
  });



  it("keeps a negative revenue day inside the hero bars' viewBox", () => {
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
    const { container } = render(<HomeRollupStrip spendCents={1400} />);
    const rects = Array.from(container.querySelectorAll('[aria-label="Revenue over the period"] rect'));
    expect(rects.length).toBe(2);
    for (const r of rects) {
      const y = Number(r.getAttribute("y"));
      const h = Number(r.getAttribute("height"));
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y + h).toBeLessThanOrEqual(20.01);
    }
  });

  it("rests a zero-revenue day's bar on the baseline, not below it", () => {
    // The clamp bug: `y` was computed from the UNCLAMPED height while the
    // rendered `height` was clamped to a 0.2 floor, so a $0 day (or any tiny
    // day beside a much larger one) drew its sliver starting BELOW the
    // baseline — for [0, big] the zero day landed at y=20, height=0.2,
    // entirely outside the 0–20 viewBox and invisible.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          days: [
            { day: "2026-08-30", revenue_microcents: 0, signups: 0, visitors: 0 },
            { day: "2026-08-31", revenue_microcents: 5_000_000, signups: 1, visitors: 1 },
          ],
        },
      }),
    );
    const { container } = render(<HomeRollupStrip spendCents={1400} />);
    const rects = Array.from(container.querySelectorAll('[aria-label="Revenue over the period"] rect'));
    expect(rects.length).toBe(2);
    const zeroY = 20; // the window is all non-negative, so the baseline sits at the bottom.
    for (const r of rects) {
      const y = Number(r.getAttribute("y"));
      const h = Number(r.getAttribute("height"));
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y + h).toBeLessThanOrEqual(20.01);
    }
    const [zeroDayRect] = rects;
    const y = Number(zeroDayRect.getAttribute("y"));
    const h = Number(zeroDayRect.getAttribute("height"));
    expect(y + h).toBeLessThanOrEqual(zeroY + 0.01);
  });

  it("keeps a zero day inside the viewBox when every day in the window is non-positive", () => {
    // In an all-non-positive window `zeroY` sits at the very top (0). A zero
    // day is treated as non-negative and drew UPWARD from the baseline
    // (zeroY - h), landing at y=-0.2 — outside the viewBox — before the clamp.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          days: [
            { day: "2026-08-30", revenue_microcents: -5_000_000, signups: 0, visitors: 0 },
            { day: "2026-08-31", revenue_microcents: 0, signups: 0, visitors: 0 },
          ],
        },
      }),
    );
    const { container } = render(<HomeRollupStrip spendCents={1400} />);
    const rects = Array.from(container.querySelectorAll('[aria-label="Revenue over the period"] rect'));
    expect(rects.length).toBe(2);
    for (const r of rects) {
      const y = Number(r.getAttribute("y"));
      const h = Number(r.getAttribute("height"));
      expect(y).toBeGreaterThanOrEqual(0);
      expect(y + h).toBeLessThanOrEqual(20.01);
    }
  });
});

describe("HomeRollupStrip — visitors, once the backend can tell the two apart", () => {
  it("says Unavailable when the owner has no PostHog connection", () => {
    // `visitors` is omitted from days[] AND named in `unavailable` for an
    // owner with no PostHog. Printing `sum() ?? 0` here would be a number
    // nobody measured.
    mockRollup.mockReturnValue(
      view({ rollup: { ...LIVE, unavailable: ["followers", "open_threads", "visitors"] } }),
    );
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.queryByLabelText("Visitors over the period")).not.toBeInTheDocument();
  });

  it("renders a MEASURED zero as a real zero, and names its source", () => {
    // The asymmetry that matters: suppressing a genuine zero-traffic day would
    // be the opposite error. An owner WITH PostHog sends `visitors: 0` that is
    // NOT in `unavailable`.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          unavailable: ["followers", "open_threads"],
          days: [
            { day: "2026-08-30", revenue_microcents: 1_000_000, signups: 1, visitors: 0 },
            { day: "2026-08-31", revenue_microcents: 2_000_000, signups: 2, visitors: 0 },
          ],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={1400} />);

    expect(screen.getByText("Visitors")).toBeInTheDocument();
    expect(screen.queryByText("Unavailable")).not.toBeInTheDocument();
    // The signal is connection-level, not per-day: a lapsed PostHog token also
    // reads as a measured 0. Naming the source is what makes that recoverable.
    expect(screen.getByText(/From your PostHog/)).toBeInTheDocument();
  });
});

describe("HomeRollupStrip — days present but nothing happened", () => {
  const ALL_ZERO = {
    ...LIVE,
    // "Nothing happened" means no spend either — a pre-revenue company with
    // costs is NOT this case, and the tiles must stay (tested below).
    agent_spend_month_to_date_microcents: 0,
    unavailable: ["followers", "open_threads", "visitors"],
    days: [
      { day: "2026-08-30", revenue_microcents: 0, signups: 0 },
      { day: "2026-08-31", revenue_microcents: 0, signups: 0 },
      { day: "2026-09-01", revenue_microcents: 0, signups: 0 },
    ],
  };

  it("collapses all-zero readings to one honest sentence, not a tile row saying $0", () => {
    // The exact live dev payload: three rows present, every value 0,
    // visitors omitted. "$0 / 0 / Unavailable · 3 days with readings" read
    // as "we have three days of data" when nothing had happened.
    mockRollup.mockReturnValue(view({ rollup: ALL_ZERO }));
    render(<HomeRollupStrip spendCents={0} />);
    expect(screen.getByText(/Nothing to report in the last 30 days/i)).toBeInTheDocument();
    expect(screen.queryByText("Revenue")).not.toBeInTheDocument();
    expect(screen.queryByText("$0")).not.toBeInTheDocument();
  });

  it("keeps the tiles when the CURRENT window is empty but a covered previous period was not", () => {
    // Revenue that stopped is the most important thing this strip can say.
    // Collapsing here threw away "$400 in the previous 30 days" and handed an
    // owner whose sales dried up the same sentence as one who never sold.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...ALL_ZERO,
          start: "2026-07-04",
          first_day: "2026-07-04",
          days: [
            { day: "2026-07-10", revenue_microcents: 400_000_000, signups: 3 },
            { day: "2026-08-30", revenue_microcents: 0, signups: 0 },
          ],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={0} />);

    expect(screen.queryByText(/Nothing to report/i)).not.toBeInTheDocument();
    // The honest picture: a real $0 for the window, beside what came before.
    // Scoped to the hero — the Agent spend tile prints "$0" here too.
    expect(within(screen.getByLabelText("Revenue")).getByText("$0")).toBeInTheDocument();
    expect(screen.getByText("$400 in the previous 30 days")).toBeInTheDocument();
    // New customers' caption now leads with the source ("Stripe customers
    // created · …"), so the comparison clause is a substring, not the
    // whole text node.
    expect(screen.getByText(/3 in the previous 30 days/)).toBeInTheDocument();
  });

  it("still renders the tiles when ANY served metric is non-zero", () => {
    // The asymmetry: $0 revenue beside 40 real visitors is real information,
    // and collapsing it would hide a measured number.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...ALL_ZERO,
          unavailable: ["followers", "open_threads"],
          days: [
            { day: "2026-08-30", revenue_microcents: 0, signups: 0, visitors: 40 },
            { day: "2026-08-31", revenue_microcents: 0, signups: 0, visitors: 12 },
          ],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={0} />);
    expect(screen.getByText("Revenue")).toBeInTheDocument();
    expect(screen.getByText("52")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing to report/i)).not.toBeInTheDocument();
  });

  it("does not let an unavailable column mask real activity elsewhere", () => {
    // visitors unavailable, but a real sale happened — tiles, not the sentence.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...ALL_ZERO,
          days: [{ day: "2026-08-30", revenue_microcents: 5_000_000, signups: 0 }],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={0} />);
    expect(screen.getByText("Revenue")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing to report/i)).not.toBeInTheDocument();
  });
});

describe("HomeRollupStrip — the two collapse cases review caught", () => {
  it("keeps the tiles when only agent spend is non-zero — pre-revenue is not 'nothing'", () => {
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          unavailable: ["followers", "open_threads", "visitors"],
          agent_spend_month_to_date_microcents: 1_400_000,
          days: [{ day: "2026-08-30", revenue_microcents: 0, signups: 0 }],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getByText("Agent spend")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing to report/i)).not.toBeInTheDocument();
  });

  it("treats a sale and a refund that net to zero as activity, not silence", () => {
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          unavailable: ["followers", "open_threads", "visitors"],
          agent_spend_month_to_date_microcents: 0,
          days: [
            { day: "2026-08-30", revenue_microcents: 5_000_000, signups: 0 },
            { day: "2026-08-31", revenue_microcents: -5_000_000, signups: 0 },
          ],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={0} />);
    expect(screen.getByText("Revenue")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing to report/i)).not.toBeInTheDocument();
  });
});

describe("HomeRollupStrip — business-first hero", () => {
  it("renders Revenue as the hero with a destination to Payments", () => {
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip spendCents={1400} />);
    const hero = screen.getByLabelText("Revenue");
    expect(hero.getAttribute("data-hero")).toBe("true");
    expect(screen.getByRole("link", { name: /Open Payments/ })).toHaveAttribute(
      "href",
      "/chat?panel=teams-payments",
    );
    expect(screen.getByRole("link", { name: /Open Customers/ })).toHaveAttribute(
      "href",
      "/chat?panel=teams-customers",
    );
    expect(screen.getByRole("link", { name: /Open Spend/ })).toHaveAttribute(
      "href",
      "/chat?panel=teams-spend",
    );
    // New customers' caption names its source before the comparison clause.
    expect(screen.getByText(/^Stripe customers created · /)).toBeInTheDocument();
  });

  it("says there is nothing to compare against, without claiming an origin", () => {
    // first_day Aug 30 is inside the current 30d window: no previous period is
    // covered. The old copy said "Readings began Aug 30" — an owner-level
    // origin claim the route never makes, since `first_day` is only the first
    // row IN THE REQUESTED RANGE (useCompanyRollup contract 3).
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getAllByText("No previous period to compare").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Readings began/)).not.toBeInTheDocument();
    expect(screen.queryByText(/in the previous 30 days/)).not.toBeInTheDocument();
  });

  it("compares against the previous period only when that period is fully covered", () => {
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          start: "2026-07-04",
          first_day: "2026-07-04",
          days: [
            { day: "2026-07-10", revenue_microcents: 400_000_000, signups: 3, visitors: 5 },
            { day: "2026-08-30", revenue_microcents: 100_000_000, signups: 1, visitors: 10 },
          ],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getByText("$400 in the previous 30 days")).toBeInTheDocument();
    // New customers' caption now leads with the source ("Stripe customers
    // created · …"), so the comparison clause is a substring, not the
    // whole text node.
    expect(screen.getByText(/3 in the previous 30 days/)).toBeInTheDocument();
    // Only the current window is summed into the figure.
    expect(screen.getByText("$100")).toBeInTheDocument();
    expect(screen.getByText("Last 30 days")).toBeInTheDocument();
  });

  it("formats a net-negative window with a leading minus, not '$-'", () => {
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          days: [{ day: "2026-08-30", revenue_microcents: -500_000_000, signups: 0, visitors: 0 }],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getByText("−$500")).toBeInTheDocument();
  });

  it("renders billed spend as Not shown — never $0 — when the costs block is absent", () => {
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip spendCents={null} />);
    expect(screen.getByText("Not shown")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open Spend/ })).toBeInTheDocument();
  });

  it("offers Connect PostHog as the exit for an unavailable Visitors tile", () => {
    mockRollup.mockReturnValue(
      view({ rollup: { ...LIVE, unavailable: ["followers", "open_threads", "visitors"] } }),
    );
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getByRole("link", { name: /Connect PostHog/ })).toHaveAttribute(
      "href",
      "/chat?panel=teams-accounts",
    );
  });

  it("tells a bound-but-unonboarded owner to finish Stripe setup, not that nothing happened", () => {
    mockStatus.mockReturnValue({ details_submitted: false, charges_enabled: false } as never);
    mockRollup.mockReturnValue(view({ rollup: { ...LIVE, days: [], first_day: null } }));
    render(<HomeRollupStrip spendCents={0} />);
    expect(screen.getByText(/finish your Stripe setup/i)).toBeInTheDocument();
    expect(screen.queryByText(/No readings yet/)).not.toBeInTheDocument();
  });

  it("keeps the period toggle mounted while the new span loads", () => {
    // Changing span re-keys SWR, so `rollup` goes undefined and `isLoading`
    // true. Hoisting the placeholder above the section replaced the tab strip
    // the owner had just clicked with an aria-hidden box — focus dropped to
    // <body> and on a slow link there was no way to change their mind.
    mockRollup.mockReturnValue(view({ rollup: undefined, isLoading: true }));
    render(<HomeRollupStrip spendCents={1400} />);
    const section = screen.getByLabelText("Business summary");
    expect(within(section).getByRole("tablist", { name: "Period" })).toBeInTheDocument();
    // The range label falls back to the span that was asked for.
    expect(within(section).getByText("Last 30 days")).toBeInTheDocument();
    const placeholder = section.querySelector('div[aria-hidden="true"]');
    expect(placeholder).not.toBeNull();
    expect(placeholder!.className).toContain("h-[220px]");
  });

  it("asks the hook for the selected span — 30 on mount, 7 after the toggle", () => {
    // The seam the toggle exists for. Radix Tabs commit on mouseDown, not
    // click, so `fireEvent.click` alone leaves the value unchanged and the
    // assertion below passes for the wrong reason.
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip spendCents={1400} />);
    const tabs = screen.getByRole("tablist", { name: "Period" });
    expect(within(tabs).getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "7d",
      "30d",
      "90d",
    ]);
    expect(mockRollup).toHaveBeenCalledWith(30);
    expect(mockRollup).not.toHaveBeenCalledWith(7);

    fireEvent.mouseDown(within(tabs).getByRole("tab", { name: "7d" }));
    expect(mockRollup).toHaveBeenCalledWith(7);
  });

  it("puts a thousands separator in the revenue hero", () => {
    // The hand-rolled formatter printed "$1234567.89". Nothing else in the
    // suite asserts a four-or-more digit dollar string, so this is the only
    // thing standing between the hero and an unreadable number.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          days: [
            { day: "2026-08-30", revenue_microcents: 1_234_567_890_000, signups: 0, visitors: 0 },
          ],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getByText("$1,234,567.89")).toBeInTheDocument();
  });

  it("holds the all-zero collapse until spend has actually been read", () => {
    // `undefined` = /dashboard still in flight. Collapsing on it told a
    // rows-but-no-sales owner "Nothing to report" and then swapped to four
    // tiles a beat later — a verdict rendered off an unread request.
    mockRollup.mockReturnValue(
      view({
        rollup: {
          ...LIVE,
          agent_spend_month_to_date_microcents: 0,
          unavailable: ["followers", "open_threads", "visitors"],
          days: [
            { day: "2026-08-30", revenue_microcents: 0, signups: 0 },
            { day: "2026-08-31", revenue_microcents: 0, signups: 0 },
          ],
        },
      }),
    );
    render(<HomeRollupStrip spendCents={undefined} />);
    expect(screen.queryByText(/Nothing to report/i)).not.toBeInTheDocument();
    expect(screen.getByText("Agent spend")).toBeInTheDocument();
    // ...and the one unread cell says so, rather than printing a figure.
    expect(screen.getByText("…")).toBeInTheDocument();
    expect(screen.queryByText("Not shown")).not.toBeInTheDocument();
  });

  it("draws the hero as bars with the exact day and amount on hover, and keeps sparklines on the tiles", () => {
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    const { container } = render(<HomeRollupStrip spendCents={1400} />);
    const hero = container.querySelector('[aria-label="Revenue over the period"]')!;
    const titles = Array.from(hero.querySelectorAll("rect > title")).map((t) => t.textContent);
    expect(titles).toEqual(["Aug 30 · $1", "Aug 31 · $2"]);
    expect(container.querySelector('[aria-label="New customers over the period"] polyline')).not.toBeNull();
    // `role="group"`, not `role="img"` — the group exposes each day's link to
    // assistive tech rather than hiding the anchors behind an image role.
    const group = screen.getByRole("group", { name: "Revenue over the period" });
    expect(within(group).getAllByRole("link")).toHaveLength(LIVE.days.length);
  });

  it("links every hero bar to Money scoped to that day, in-shell with a real href", () => {
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    const { container } = render(<HomeRollupStrip spendCents={1400} />);
    const links = Array.from(container.querySelectorAll('[aria-label="Revenue over the period"] a'));
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/chat?panel=teams-money&day=2026-08-30",
      "/chat?panel=teams-money&day=2026-08-31",
    ]);
    expect(links[0].querySelector("rect")).not.toBeNull();
  });

  it("calls the customer tile New customers and lets the hero span two columns beside the tiles", () => {
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip spendCents={1400} />);
    expect(screen.getByRole("group", { name: "New customers" })).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Signups" })).not.toBeInTheDocument();
    const hero = screen.getByRole("group", { name: "Revenue" });
    expect(hero.className).toContain("@3xl:row-span-3");
    expect(hero.className).not.toContain("col-span-3");
  });

  it("hands each tile's question to the CEO with the period in it", () => {
    mockRollup.mockReturnValue(view({ rollup: LIVE }));
    render(<HomeRollupStrip spendCents={1400} />);
    const revenue = screen.getByRole("group", { name: "Revenue" });
    fireEvent.click(within(revenue).getByRole("button", { name: "Ask CEO" }));
    expect(askCeo).toHaveBeenCalledWith(expect.stringMatching(/revenue.*30 days/));
    fireEvent.click(within(screen.getByRole("group", { name: "Agent spend" })).getByRole("button", { name: "Ask CEO" }));
    expect(askCeo).toHaveBeenLastCalledWith(expect.stringMatching(/agents cost/));
  });
});
