import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

const hook = vi.hoisted(() => ({ impl: vi.fn() }));
vi.mock("@/hooks/useCompanyMoney", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/hooks/useCompanyMoney")>();
  return { ...mod, useCompanyMoney: (...a: unknown[]) => hook.impl(...a) };
});

import { PaymentsSection } from "../PaymentsSection";
import { CustomersSection } from "../CustomersSection";
import { ProductsSection } from "../ProductsSection";
import { formatIsoDay } from "../money";

const rowsView = (rows: unknown[], hasMore = false) => ({ rows, view: { kind: rows.length ? "rows" : "zero" }, hasMore, next: vi.fn() });

describe("Money sections", () => {
  beforeEach(() => vi.clearAllMocks());

  it("Payments: newest first, receipt links, Show more only when Stripe says there is more", () => {
    const next = vi.fn();
    hook.impl.mockReturnValue({ ...rowsView([
      { id: "ch_1", amount: 8900, amount_refunded: 0, currency: "usd", created: 1788600000, status: "succeeded", paid: true, refunded: false, disputed: false, description: "Fall Candle Trio", receipt_url: "https://r/1", customer: null },
    ], true), next });
    render(<PaymentsSection day={null} onDayCleared={vi.fn()} />);
    const section = screen.getByRole("region", { name: "Payments" });
    expect(within(section).getByText("Fall Candle Trio")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Receipt" })).toHaveAttribute("href", "https://r/1");
    fireEvent.click(within(section).getByRole("button", { name: "Show more" }));
    expect(next).toHaveBeenCalled();
    expect(hook.impl).toHaveBeenCalledWith("payments", undefined);
  });

  it("Payments scoped to a day: names the day, reads that window, and offers the way back", () => {
    const cleared = vi.fn();
    hook.impl.mockReturnValue(rowsView([]));
    render(<PaymentsSection day="2026-09-05" onDayCleared={cleared} />);
    expect(hook.impl).toHaveBeenCalledWith("payments", { start: "2026-09-05", end: "2026-09-05" });
    expect(screen.getByText("Payments on Sep 5, 2026")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("No payments on Sep 5, 2026.");
    fireEvent.click(screen.getByRole("button", { name: "Show all payments" }));
    expect(cleared).toHaveBeenCalled();
  });

  it("Customers: shows the 30-day count only when it was read, and never a total", () => {
    hook.impl.mockReturnValue(rowsView([{ id: "cus_1", email: "h@x.com", name: "Hannah Reyes", created: 1788600000, currency: "usd", delinquent: false, balance: 0 }]));
    const { rerender } = render(<CustomersSection newIn30Days={6} />);
    expect(screen.getByText("6 new in 30 days")).toBeInTheDocument();
    expect(screen.queryByText(/Show all/)).toBeNull();
    rerender(<CustomersSection newIn30Days={null} />);
    expect(screen.queryByText(/new in 30 days/)).toBeNull();
  });

  it("Products: is titled What you sell and offers Add a product only when Stripe is bound", () => {
    hook.impl.mockReturnValue(rowsView([{ id: "prod_1", name: "Gift set", active: true, description: null, created: 1, default_price: { id: "p", unit_amount: 12000, currency: "usd" } }]));
    const { rerender } = render(<ProductsSection stripeBound />);
    expect(screen.getByRole("region", { name: "What you sell" })).toBeInTheDocument();
    expect(screen.getByText("$120.00")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Add a product/ })).toHaveAttribute("href", "https://dashboard.stripe.com/products/create");
    rerender(<ProductsSection stripeBound={false} />);
    expect(screen.queryByRole("link", { name: /Add a product/ })).toBeNull();
  });

  it("empty lists say so in one calm line", () => {
    hook.impl.mockReturnValue(rowsView([]));
    render(<CustomersSection newIn30Days={0} />);
    expect(screen.getByRole("status")).toHaveTextContent("No customers yet.");
  });

  it("formats an ISO day in UTC", () => {
    expect(formatIsoDay("2026-09-05")).toBe("Sep 5, 2026");
  });
});
