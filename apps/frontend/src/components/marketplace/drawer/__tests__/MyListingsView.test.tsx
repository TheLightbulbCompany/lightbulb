import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import { MyListingsView } from "../MyListingsView";
import type { Listing } from "@/lib/marketplace/types";

const mockMyListings = vi.fn();
vi.mock("@/lib/marketplace/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/marketplace/api")>(
    "@/lib/marketplace/api",
  );
  return {
    ...actual,
    myListings: (...args: unknown[]) => mockMyListings(...args),
  };
});

const mockApiGet = vi.fn();
const mockApiPost = vi.fn();
vi.mock("@/lib/api", () => ({
  useApi: () => ({
    get: mockApiGet,
    post: mockApiPost,
    put: vi.fn(),
    del: vi.fn(),
  }),
}));

// Default: personal user (no org membership) — admin-gated CTAs render.
// Individual tests can override via `mockUseOrganization.mockReturnValue(...)`.
const mockUseOrganization = vi.fn(() => ({ membership: null as null | { role: string } }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    userId: "u_seller",
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
  }),
  useOrganization: () => mockUseOrganization(),
}));

// Hoisted spies so each test body can both inject them into the mocked
// provider and assert against them. vi.mock runs before module init, so
// plain const won't do.
const { openListingDetailSpy, setViewSpy, startEditListingSpy } = vi.hoisted(() => ({
  openListingDetailSpy: vi.fn(),
  setViewSpy: vi.fn(),
  startEditListingSpy: vi.fn(),
}));

// MyListingsView only consumes `useMarketplaceDrawer` — no need to
// re-export the provider component from the mock. The hoisted spies let
// each test inject + assert on the drawer transitions.
vi.mock("../MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    openListingDetail: openListingDetailSpy,
    setView: setViewSpy,
    startEditListing: startEditListingSpy,
  }),
}));

function listing(overrides: Partial<Listing> = {}): Listing {
  return {
    listing_id: "lst_1",
    slug: "ray",
    name: "Ray",
    description_md: "A research-ops agent",
    format: "openclaw",
    price_cents: 0,
    tags: [],
    seller_id: "u_seller",
    status: "draft",
    version: 1,
    published_at: null,
    created_at: "2026-05-10T12:00:00Z",
    ...overrides,
  };
}

describe("MyListingsView", () => {
  beforeEach(() => {
    mockMyListings.mockReset();
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    openListingDetailSpy.mockReset();
    setViewSpy.mockReset();
    startEditListingSpy.mockReset();
    // /marketplace/payouts/dashboard is fetched alongside the listings.
    mockApiGet.mockResolvedValue({ dashboard_url: null, balance_held_cents: 0 });
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("groups rows by listing_id and renders one row per listing", async () => {
    mockMyListings.mockResolvedValueOnce({
      items: [
        listing({
          listing_id: "lst_a",
          version: 1,
          status: "published",
          name: "Agent A",
          created_at: "2026-05-10T12:00:00Z",
        }),
        listing({
          listing_id: "lst_a",
          version: 2,
          status: "review",
          name: "Agent A",
          created_at: "2026-05-15T12:00:00Z",
        }),
        listing({
          listing_id: "lst_b",
          version: 1,
          status: "published",
          name: "Agent B",
          created_at: "2026-05-12T12:00:00Z",
        }),
      ],
    });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getAllByTestId("my-listings-row")).toHaveLength(2));
  });

  it("Live row with v2 in review shows combined pill", async () => {
    mockMyListings.mockResolvedValueOnce({
      items: [
        listing({
          listing_id: "lst_a",
          version: 1,
          status: "published",
          name: "Agent A",
          created_at: "2026-05-10T12:00:00Z",
        }),
        listing({
          listing_id: "lst_a",
          version: 2,
          status: "review",
          name: "Agent A",
          created_at: "2026-05-15T12:00:00Z",
        }),
      ],
    });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByTestId("my-listings-row")).toBeInTheDocument());
    // Single pill renders "Live · v2 in review" — assert both fragments.
    expect(screen.getByText(/Live/)).toBeInTheDocument();
    expect(screen.getByText(/v2 in review/i)).toBeInTheDocument();
  });

  it("Live row with v2 rejected shows combined pill", async () => {
    mockMyListings.mockResolvedValueOnce({
      items: [
        listing({
          listing_id: "lst_a",
          version: 1,
          status: "published",
          name: "Agent A",
          created_at: "2026-05-10T12:00:00Z",
        }),
        listing({
          listing_id: "lst_a",
          version: 2,
          status: "draft",
          name: "Agent A",
          created_at: "2026-05-15T12:00:00Z",
          rejection_notes: "Needs more detail.",
        }),
      ],
    });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByTestId("my-listings-row")).toBeInTheDocument());
    expect(screen.getByText(/Live/)).toBeInTheDocument();
    expect(screen.getByText(/v2 rejected/i)).toBeInTheDocument();
  });

  it("clicking a row opens the listing detail view", async () => {
    mockMyListings.mockResolvedValueOnce({
      items: [
        listing({
          listing_id: "lst_a",
          version: 1,
          status: "published",
          name: "Agent A",
        }),
      ],
    });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByTestId("my-listings-row")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("my-listings-row"));
    expect(openListingDetailSpy).toHaveBeenCalledWith("lst_a");
  });

  it("does not render the inline Edit button anymore — detail view is the single door", async () => {
    mockMyListings.mockResolvedValueOnce({
      items: [
        listing({
          listing_id: "lst_a",
          status: "draft",
          name: "Draft agent",
        }),
      ],
    });
    render(<MyListingsView />);
    await waitFor(() => expect(screen.getByText("Draft agent")).toBeInTheDocument());
    expect(screen.queryByTestId("edit-listing")).toBeNull();
  });

  it("renders Live pill for a standalone published listing with no pending revision", async () => {
    mockMyListings.mockResolvedValueOnce({
      items: [
        listing({
          listing_id: "lst_solo",
          status: "published",
          name: "Solo Live",
        }),
      ],
    });
    render(<MyListingsView />);
    await screen.findByText("Solo Live");
    // No "·" suffix when nothing is pending.
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.queryByText(/in review/i)).toBeNull();
    expect(screen.queryByText(/rejected/i)).toBeNull();
  });

  it("still renders rows for listings with no published version (e.g. fresh draft only)", async () => {
    mockMyListings.mockResolvedValueOnce({
      items: [
        listing({
          listing_id: "lst_fresh",
          status: "draft",
          name: "Fresh Draft",
        }),
      ],
    });
    render(<MyListingsView />);
    await screen.findByText("Fresh Draft");
    // Fresh draft (no published row exists yet) is adopted as the canonical
    // row so the seller can drill in and continue work.
    expect(screen.getByTestId("my-listings-row")).toBeInTheDocument();
  });
});

describe("MyListingsView — org-admin gate", () => {
  beforeEach(() => {
    mockMyListings.mockReset();
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    openListingDetailSpy.mockReset();
    setViewSpy.mockReset();
    startEditListingSpy.mockReset();
    mockApiGet.mockResolvedValue({ dashboard_url: null, balance_held_cents: 0 });
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("renders the restricted view for non-admin org members and skips the fetch", async () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    render(<MyListingsView />);
    expect(await screen.findByTestId("marketplace-restricted")).toBeInTheDocument();
    // Skipping the fetch is the point — if we ever 403 from /listings or
    // /payouts/dashboard the seller drawer would surface an error toast.
    expect(mockMyListings).not.toHaveBeenCalled();
    expect(mockApiGet).not.toHaveBeenCalled();
    // The publish CTA is gone.
    expect(screen.queryByRole("button", { name: /New listing/i })).toBeNull();
  });

  it("renders the publish CTA for personal users (no active org)", async () => {
    mockUseOrganization.mockReturnValue({ membership: null });
    mockMyListings.mockResolvedValue({ items: [] });
    render(<MyListingsView />);
    expect(
      await screen.findByRole("button", { name: /Publish your agent/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
  });

  it("renders the publish CTA for org admins", async () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:admin" } });
    mockMyListings.mockResolvedValue({ items: [] });
    render(<MyListingsView />);
    expect(
      await screen.findByRole("button", { name: /Publish your agent/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
  });
});
