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

const rowsView = (rows: unknown[], hasMore = false, stripeBound = true) => ({ rows, view: { kind: rows.length ? "rows" : "zero" }, hasMore, stripeBound, next: vi.fn() });

const payment = (id: string, description: string) => ({ id, amount: 8900, amount_refunded: 0, currency: "usd", created: 1788600000, status: "succeeded", paid: true, refunded: false, disputed: false, description, receipt_url: `https://r/${id}`, customer: null });

describe("Money sections", () => {
  beforeEach(() => vi.clearAllMocks());

  it("Payments: a five-row preview with receipt links, and the full ledger in Stripe — never a pager", () => {
    // "A list of all of the payments doesn't help": unscoped, this is a
    // preview. Reconciling line by line is what the Stripe Dashboard is for.
    hook.impl.mockReturnValue(rowsView(Array.from({ length: 8 }, (_, i) => payment(`ch_${i}`, `Order ${i}`)), true));
    render(<PaymentsSection day={null} onDayCleared={vi.fn()} />);
    const section = screen.getByRole("region", { name: "Payments" });
    expect(within(section).getAllByRole("link", { name: "Receipt" })).toHaveLength(5);
    expect(within(section).getByText("Order 4")).toBeInTheDocument();
    expect(within(section).queryByText("Order 5")).toBeNull();
    expect(within(section).getByText("5 most recent")).toBeInTheDocument();
    // Stripe says there IS more, and the preview still refuses to page.
    expect(within(section).queryByRole("button", { name: "Show more" })).toBeNull();
    expect(within(section).getByRole("link", { name: /Full ledger in Stripe/ })).toHaveAttribute("href", "https://dashboard.stripe.com/payments");
    expect(hook.impl).toHaveBeenCalledWith("payments", undefined);
  });

  it("Payments: offers no Stripe ledger link when there is no account behind it", () => {
    hook.impl.mockReturnValue(rowsView([], false, false));
    render(<PaymentsSection day={null} onDayCleared={vi.fn()} />);
    expect(screen.queryByRole("link", { name: /Full ledger in Stripe/ })).toBeNull();
  });

  it("Payments scoped to a day: keeps the full list and its pager", () => {
    // The owner asked for that day specifically, so this one is not a preview.
    const next = vi.fn();
    hook.impl.mockReturnValue({ ...rowsView(Array.from({ length: 8 }, (_, i) => payment(`ch_${i}`, `Order ${i}`)), true), next });
    render(<PaymentsSection day="2026-09-05" onDayCleared={vi.fn()} />);
    const section = screen.getByRole("region", { name: "Payments on Sep 5, 2026" });
    expect(within(section).getAllByRole("link", { name: "Receipt" })).toHaveLength(8);
    fireEvent.click(within(section).getByRole("button", { name: "Show more" }));
    expect(next).toHaveBeenCalled();
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
