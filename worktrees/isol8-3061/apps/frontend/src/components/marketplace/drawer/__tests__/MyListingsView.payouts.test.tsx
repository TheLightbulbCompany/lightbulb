import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const myListingsFn = vi.fn();
const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("@/lib/marketplace/api", () => ({
  myListings: (opts: unknown) => myListingsFn(opts),
}));
vi.mock("@/lib/api", () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "u_seller", getToken: vi.fn().mockResolvedValue("tok_abc") }),
  // useMarketplaceWriteGate (the new admin gate) reads useOrganization.
  // null membership = personal user → gate open.
  useOrganization: () => ({ membership: null }),
}));

const setView = vi.fn();
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ setView }),
}));

import { MyListingsView } from "@/components/marketplace/drawer/MyListingsView";

const SAMPLE_LISTINGS = [
  { listing_id: "l1", slug: "atlas", name: "Atlas", status: "published", price_cents: 1200,
    install_count: 847, updated_at: "2026-05-05", description_md: "", seller_id: "u_seller",
    format: "openclaw", tags: [], version: 1, created_at: "2026-05-01", published_at: "2026-05-01" },
  { listing_id: "l2", slug: "tally", name: "Tally", status: "review", price_cents: 0,
    install_count: 0, updated_at: "2026-05-07", description_md: "", seller_id: "u_seller",
    format: "openclaw", tags: [], version: 1, created_at: "2026-05-07", published_at: null },
];

describe("MyListingsView", () => {
  beforeEach(() => {
    myListingsFn.mockReset();
    apiGet.mockReset();
    apiPost.mockReset();
    setView.mockReset();
  });

  it("renders three stats and listings list", async () => {
    myListingsFn.mockResolvedValueOnce({ items: SAMPLE_LISTINGS });
    apiGet.mockResolvedValueOnce({ balance_held_cents: 0, dashboard_url: null });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText("Atlas")).toBeInTheDocument());
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("847")).toBeInTheDocument();
    expect(screen.getByText("$0.00")).toBeInTheDocument();
    expect(screen.getByText(/in review/i)).toBeInTheDocument();
    expect(screen.getByText(/^live$/i)).toBeInTheDocument();
  });

  it("shows 'Set up payouts' when dashboard_url is null", async () => {
    myListingsFn.mockResolvedValueOnce({ items: SAMPLE_LISTINGS });
    apiGet.mockResolvedValueOnce({ balance_held_cents: 0, dashboard_url: null });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText(/set up payouts/i)).toBeInTheDocument());
  });

  it("shows 'Cash out' when dashboard_url exists", async () => {
    myListingsFn.mockResolvedValueOnce({ items: SAMPLE_LISTINGS });
    apiGet.mockResolvedValueOnce({
      balance_held_cents: 2430,
      dashboard_url: "https://dashboard.stripe.com/abc",
    });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText(/cash out/i)).toBeInTheDocument());
    expect(screen.getByText("$24.30")).toBeInTheDocument();
  });

  it("'Set up payouts' click triggers /payouts/onboard then redirects", async () => {
    myListingsFn.mockResolvedValueOnce({ items: [] });
    apiGet.mockResolvedValueOnce({ balance_held_cents: 0, dashboard_url: null });
    apiPost.mockResolvedValueOnce({ onboarding_url: "https://connect.stripe.com/setup/xyz" });

    const user = userEvent.setup();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      value: { ...originalLocation, href: "" },
      writable: true,
    });

    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText(/set up payouts/i)).toBeInTheDocument());
    await user.click(screen.getByText(/set up payouts/i));
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith("/marketplace/payouts/onboard"));
    await waitFor(() => expect(window.location.href).toContain("connect.stripe.com"));
  });

  it("'+ New listing' switches to publish view", async () => {
    myListingsFn.mockResolvedValueOnce({ items: [] });
    apiGet.mockResolvedValueOnce({ balance_held_cents: 0, dashboard_url: null });
    const user = userEvent.setup();
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByRole("button", { name: /new listing/i })).toBeVisible());
    await user.click(screen.getByRole("button", { name: /new listing/i }));
    expect(setView).toHaveBeenCalledWith("publish");
  });
});
