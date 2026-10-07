// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

// The span argument is asserted, not swallowed: the page fetches ONE window
// (90 → 180 rows) and the toggle slices it, so a hook called with 7 would mean
// a second request for rows already in hand.
const mockRollup = vi.fn();
vi.mock("@/hooks/useCompanyRollup", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/hooks/useCompanyRollup")>();
  return { ...mod, useCompanyRollup: (span: number) => mockRollup(span) };
});

import { RevenueSection, widenToCover } from "../RevenueSection";

// end = 2026-09-08. The 30d window starts 2026-08-10 and its previous period
// starts 2026-07-11, which first_day reaches back past — so the comparison is
// covered. The 7d window starts 2026-09-02, which drops the Aug 31 row.
const LIVE = {
  start: "2026-03-11",
  end: "2026-09-08",
  stripe_bound: true,
  first_day: "2026-06-01",
  unavailable: [] as string[],
  agent_spend_month_to_date_microcents: 0,
  days: [
    { day: "2026-07-20", revenue_microcents: 400_000_000, signups: 0, visitors: 0 }, // previous 30d
    { day: "2026-08-31", revenue_microcents: 90_000_000, signups: 0, visitors: 0 },
    { day: "2026-09-02", revenue_microcents: 34_000_000, signups: 0, visitors: 0 },
    { day: "2026-09-05", revenue_microcents: 640_000_000, signups: 0, visitors: 0 },
  ],
};

const view = (over: Record<string, unknown> = {}) => ({
  rollup: LIVE,
  windowStart: "2026-06-09",
  span: 90,
  error: undefined,
  isLoading: false,
  refresh: vi.fn(),
  isUnavailable: (key: string) =>
    (((over.rollup ?? LIVE) as { unavailable?: string[] }).unavailable ?? []).includes(key),
  ...over,
});

beforeEach(() => mockRollup.mockReset());

describe("Money's revenue chart", () => {
  it("totals the selected period from rollup days and compares it with the previous one", () => {
    mockRollup.mockReturnValue(view());
    render(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    // 90 + 34 + 640 — the July row is the PREVIOUS period and must not be in
    // the figure. Cursor pages carry no totals, so nothing else may be summed.
    expect(screen.getByText("$764")).toBeInTheDocument();
    expect(screen.getByText("Up $364 from $400 in the previous 30 days")).toBeInTheDocument();
  });

  it("slices the period client-side — one fetch, and the toggle changes the window", () => {
    mockRollup.mockReturnValue(view());
    render(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    expect(mockRollup).toHaveBeenCalledWith(90);
    // Radix Tabs commit on mouseDown; fireEvent.click alone leaves the value
    // unchanged and the assertion below would pass for the wrong reason.
    const tabs = screen.getByRole("tablist", { name: "Period" });
    fireEvent.mouseDown(within(tabs).getByRole("tab", { name: "7d" }));
    // 7d window is 2026-09-02..2026-09-08, so the Aug 31 row drops out: 34 + 640.
    expect(screen.getByText("$674")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Revenue over the period" }).querySelectorAll("rect").length).toBe(2);
    // ...and it never asked for a second span.
    expect(mockRollup).not.toHaveBeenCalledWith(7);
  });

  it("says there is nothing to compare when the previous period is not covered", () => {
    // first_day inside the current window: the previous 30 days were never
    // read, and there is no backfill, so a $0 comparison would be invented.
    mockRollup.mockReturnValue(view({ rollup: { ...LIVE, first_day: "2026-08-31" } }));
    render(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    expect(screen.getByText("No previous 30 days to compare against")).toBeInTheDocument();
    expect(screen.getByText("Since Aug 31")).toBeInTheDocument();
  });

  it("scopes the page to a day when a bar is clicked, and offers the way back", () => {
    mockRollup.mockReturnValue(view());
    const cleared = vi.fn();
    const { rerender } = render(<RevenueSection day={null} onDayCleared={cleared} />);
    const bars = screen.getByRole("group", { name: "Revenue over the period" });
    const links = within(bars).getAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/chat?panel=teams-money&day=2026-08-31",
      "/chat?panel=teams-money&day=2026-09-02",
      "/chat?panel=teams-money&day=2026-09-05",
    ]);
    fireEvent.click(links[2]);
    expect(window.location.search).toBe("?panel=teams-money&day=2026-09-05");

    rerender(<RevenueSection day="2026-09-05" onDayCleared={cleared} />);
    expect(screen.getByText("Showing Sep 5, 2026")).toBeInTheDocument();
    // The scoped day takes the accent instead of "today".
    expect(within(screen.getByRole("group", { name: "Revenue over the period" })).getByRole("link", { current: true }))
      .toHaveAttribute("href", "/chat?panel=teams-money&day=2026-09-05");
    fireEvent.click(screen.getByRole("button", { name: "Show the whole period" }));
    expect(cleared).toHaveBeenCalled();
  });

  it("hovering a bar gives the exact day and amount", () => {
    mockRollup.mockReturnValue(view());
    const { container } = render(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    expect(Array.from(container.querySelectorAll("rect > title")).map((t) => t.textContent)).toEqual([
      "Aug 31 · $90",
      "Sep 2 · $34",
      "Sep 5 · $640",
    ]);
  });

  it("draws no chart when nothing has been rolled up, and none when Stripe is unbound", () => {
    mockRollup.mockReturnValue(view({ rollup: { ...LIVE, first_day: null, days: [] } }));
    const { rerender } = render(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    expect(screen.queryByRole("group", { name: "Revenue over the period" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("No daily readings yet.");
    expect(screen.queryByText("$0")).toBeNull();

    mockRollup.mockReturnValue(view({ rollup: { ...LIVE, stripe_bound: false, days: [], first_day: null } }));
    rerender(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(/Connect Stripe/);
    expect(screen.queryByRole("group", { name: "Revenue over the period" })).toBeNull();
  });

  it("says a failed read failed — role=alert, never a zero", () => {
    mockRollup.mockReturnValue(view({ rollup: undefined, error: new Error("boom") }));
    render(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/temporary read failure/);
    expect(screen.queryByText("$0")).toBeNull();
  });

  it("renders an unavailable revenue column as unavailable, never as 0", () => {
    mockRollup.mockReturnValue(view({ rollup: { ...LIVE, unavailable: ["revenue_microcents"] } }));
    render(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Revenue isn't available for this account.");
    expect(screen.queryByRole("group", { name: "Revenue over the period" })).toBeNull();
  });

  it("keeps the period toggle mounted while the first read is in flight", () => {
    mockRollup.mockReturnValue(view({ rollup: undefined, isLoading: true }));
    render(<RevenueSection day={null} onDayCleared={vi.fn()} />);
    expect(screen.getByRole("tablist", { name: "Period" })).toBeInTheDocument();
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("widenToCover — a ?day deep link outside the chosen period", () => {
  const END = "2026-09-06";

  it("leaves the chosen period alone when it already contains the day", () => {
    expect(widenToCover("2026-09-01", 30, END)).toBe(30);
    expect(widenToCover("2026-09-05", 7, END)).toBe(7);
  });

  it("widens to the smallest period that contains the day", () => {
    // 45 days back: outside 7 and 30, inside 90.
    expect(widenToCover("2026-07-23", 7, END)).toBe(90);
    // 20 days back: outside 7, inside 30.
    expect(widenToCover("2026-08-17", 7, END)).toBe(30);
  });

  it("falls back to the widest period when nothing covers the day", () => {
    // Older than the 90-day window — the day is outside the fetch entirely.
    expect(widenToCover("2025-01-01", 7, END)).toBe(90);
  });

  it("never narrows below what the owner chose", () => {
    expect(widenToCover("2026-09-06", 90, END)).toBe(90);
  });

  it("keeps the chosen period when the range end is not known yet", () => {
    expect(widenToCover("2026-07-01", 30, null)).toBe(30);
  });
});
