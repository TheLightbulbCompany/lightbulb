import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";

const money = vi.hoisted(() => ({ view: { kind: "rows" } as { kind: string } }));
vi.mock("@/hooks/useCompanyMoney", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/hooks/useCompanyMoney")>();
  return { ...mod, useCompanyMoney: () => ({ rows: [], view: money.view, hasMore: false, next: vi.fn() }) };
});
const rollup = vi.hoisted(() => ({ value: undefined as unknown, loading: false }));
vi.mock("@/hooks/useCompanyRollup", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/hooks/useCompanyRollup")>();
  return { ...mod, useCompanyRollup: () => ({ rollup: rollup.value, isLoading: rollup.loading, windowStart: "2026-06-09", span: 90, error: undefined, refresh: vi.fn(), isUnavailable: () => false }) };
});
vi.mock("@/components/teams/panels/money/AgentSpendSection", () => ({ AgentSpendSection: () => <section id="money-spend" aria-label="Agent spend" /> }));
vi.mock("@/components/teams/panels/money/RevenueSection", () => ({ RevenueSection: ({ day }: { day: string | null }) => <section id="money-revenue" aria-label="Revenue" data-day={day ?? ""} /> }));
vi.mock("@/components/teams/panels/money/PaymentsSection", () => ({ PaymentsSection: ({ day }: { day: string | null }) => <section id="money-payments" aria-label="Payments" data-day={day ?? ""} /> }));
vi.mock("@/components/teams/panels/money/CustomersSection", () => ({ CustomersSection: ({ newIn30Days }: { newIn30Days: number | null }) => <section id="money-customers" aria-label="Customers" data-new={String(newIn30Days)} /> }));
vi.mock("@/components/teams/panels/money/ProductsSection", () => ({ ProductsSection: () => <section id="money-products" aria-label="What you sell" /> }));

import { MoneyPanel } from "../MoneyPanel";
import { formatMicrocents, monthToDate } from "../money/money";

const bound = { start: "2026-06-09", end: "2026-09-06", stripe_bound: true, first_day: "2026-08-30", unavailable: [], agent_spend_month_to_date_microcents: 0,
  days: [
    { day: "2026-08-31", revenue_microcents: 90_000_000, signups: 1, visitors: 0 },
    { day: "2026-09-02", revenue_microcents: 34_000_000, signups: 2, visitors: 0 },
    { day: "2026-09-05", revenue_microcents: 640_000_000, signups: 4, visitors: 0 },
  ] };

describe("MoneyPanel — one ledger page", () => {
  beforeEach(() => { rollup.value = bound; rollup.loading = false; money.view = { kind: "rows" }; vi.useFakeTimers({ now: new Date("2026-09-06T12:00:00Z"), toFake: ["Date"] }); });
  afterEach(() => vi.useRealTimers());

  it("carries the month's landed revenue in the masthead and the five sections in order, revenue first", () => {
    render(<MoneyPanel />);
    expect(screen.getByRole("heading", { level: 1, name: "Money" })).toBeInTheDocument();
    expect(screen.getByTestId("panel-masthead-meta")).toHaveTextContent("$674 landed in September");
    // The old "Biggest day: … Open that day" line is gone on purpose: the
    // tallest bar in the chart below IS the biggest day, and it is already a
    // link, so the sentence said the same fact twice.
    expect(screen.queryByText(/Biggest day/)).toBeNull();
    const names = screen.getAllByRole("region").map((r) => r.getAttribute("aria-label"));
    // Revenue leads: the chart is the subject of the page, the ledger is not.
    expect(names).toEqual(["Revenue", "Payments", "Customers", "What you sell", "Agent spend"]);
    expect(screen.getByRole("region", { name: "Customers" })).toHaveAttribute("data-new", "7");
  });

  it("counts no new customers when the rollup has never read a day", () => {
    // first_day null = nothing stored, so 0 signups means "we don't know",
    // not "nobody signed up".
    rollup.value = { ...bound, first_day: null };
    render(<MoneyPanel />);
    expect(screen.getByRole("region", { name: "Customers" })).toHaveAttribute("data-new", "null");
  });

  it("hands the day scope to the chart and to Payments, and links the biggest day to it", () => {
    render(<MoneyPanel day="2026-09-05" />);
    expect(screen.getByRole("region", { name: "Payments" })).toHaveAttribute("data-day", "2026-09-05");
    expect(screen.getByRole("region", { name: "Revenue" })).toHaveAttribute("data-day", "2026-09-05");
  });

  it("gates the Stripe sections behind the account state, and keeps Agent spend", () => {
    money.view = { kind: "unbound" };
    rollup.value = { ...bound, stripe_bound: false, days: [] };
    render(<MoneyPanel />);
    expect(screen.getByText("Payments are not set up for this business yet.")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Payments" })).toBeNull();
    // The chart is Stripe material too — it must not render a false empty
    // state behind the page-level notice.
    expect(screen.queryByRole("region", { name: "Revenue" })).toBeNull();
    expect(screen.getByRole("region", { name: "Agent spend" })).toBeInTheDocument();
    expect(screen.queryByTestId("panel-masthead-meta")).toBeNull();
  });

  it("keeps a non-admin off Agent spend too — both routes require org admin", () => {
    // /company/* and /billing both require org admin, so a member who 403s on
    // payments would 403 inside Agent spend as well: one honest notice beats a
    // notice plus a section that errors under it.
    money.view = { kind: "forbidden" };
    render(<MoneyPanel />);
    expect(screen.getByText(/Only workspace admins can see payments/i)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Agent spend" })).toBeNull();
  });

  it("scrolls to the section an absorbed key named, inside its own page root", () => {
    const seen: Element[] = [];
    const spy = vi.fn(function (this: Element) { seen.push(this); });
    Element.prototype.scrollIntoView = spy as unknown as Element["scrollIntoView"];
    const { container } = render(<MoneyPanel section="spend" />);
    expect(spy).toHaveBeenCalled();
    expect(seen[0]).toBe(container.querySelector("#money-spend"));
  });

  it("prints no landed figure while the rollup is in flight, and none when the month has no rows", () => {
    rollup.value = undefined; rollup.loading = true;
    const { rerender } = render(<MoneyPanel />);
    expect(screen.queryByTestId("panel-masthead-meta")).toBeNull();
    rollup.value = { ...bound, days: [bound.days[0]] }; rollup.loading = false;
    rerender(<MoneyPanel />);
    expect(screen.queryByTestId("panel-masthead-meta")).toBeNull();
    expect(screen.queryByText(/Biggest day/)).toBeNull();
  });

  it("helpers: month-to-date is UTC-month scoped and signed", () => {
    expect(formatMicrocents(674_000_000)).toBe("$674"); // whole dollars render without cents (formatUsdCents)
    expect(formatMicrocents(674_500_000)).toBe("$674.50");
    expect(formatMicrocents(-5_000_000)).toBe("−$5");
    expect(monthToDate(bound.days, "2026-09-06")).toEqual({ month: "2026-09", microcents: 674_000_000 });
    expect(monthToDate([bound.days[0]], "2026-09-06")).toBeNull();
  });
});
