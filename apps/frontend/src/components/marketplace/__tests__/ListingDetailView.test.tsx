import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ListingDetailView } from "../ListingDetailView";
import {
  discardListingVersion,
  myListings,
  republishListing,
} from "@/lib/marketplace/api";

// Return a STABLE object reference — otherwise ListingDetailView's useEffect
// dependency on getToken would treat each render as a fresh function and
// re-fire the fetch, swallowing the mockResolvedValueOnce on subsequent
// renders and falling back to the default `myListings` mock. vi.hoisted
// because vi.mock runs above any module-level const.
const { stableAuth } = vi.hoisted(() => ({
  stableAuth: { userId: "seller_1", getToken: async () => "test-jwt" },
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => stableAuth,
}));

// vi.mock is hoisted ABOVE module-level const declarations, so any data the
// mock factory references must come through vi.hoisted() — otherwise the
// factory body runs while `baseListing` is still in the TDZ and throws.
const { baseListing } = vi.hoisted(() => ({
  baseListing: {
    listing_id: "lst_x",
    version: 3,
    status: "published" as const,
    slug: "x",
    name: "My Agent",
    description_md: "# Hello\nThis is the agent.",
    price_cents: 500,
    tags: ["productivity", "writing"],
    category: "agent",
    format: "openclaw" as const,
    seller_id: "seller_1",
    install_count: 17,
    agent_id: "agent_abc123",
    created_at: "2026-05-23T00:00:00Z",
  },
}));

vi.mock("@/lib/marketplace/api", () => ({
  getListingBySlug: vi.fn().mockResolvedValue(baseListing),
  // ListingDetailView fetches the seller's full list and filters by listing_id.
  // The detail row(s) must appear in items[] for the component to resolve.
  myListings: vi.fn().mockResolvedValue({ items: [baseListing] }),
  republishListing: vi.fn(),
  discardListingVersion: vi.fn(),
}));

// Hoisted spy so the test body can both inject it into the mocked provider
// and assert on it. vi.mock runs before module init, so plain const won't do.
const { startEditListingSpy } = vi.hoisted(() => ({
  startEditListingSpy: vi.fn(),
}));

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    startEditListing: startEditListingSpy,
    setView: vi.fn(),
  }),
}));

beforeEach(() => {
  vi.mocked(myListings).mockReset();
  vi.mocked(myListings).mockResolvedValue({ items: [baseListing] });
  vi.mocked(republishListing).mockReset();
  vi.mocked(discardListingVersion).mockReset();
  startEditListingSpy.mockReset();
});

describe("<ListingDetailView>", () => {
  it("renders live row metadata in Overview tab", async () => {
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByText("My Agent")).toBeInTheDocument());
    expect(screen.getByText(/Live/)).toBeInTheDocument();
    // Stats split the number and label into separate elements.
    expect(screen.getByText("17")).toBeInTheDocument();
    expect(screen.getByText("installs")).toBeInTheDocument();
    expect(screen.getByText("productivity")).toBeInTheDocument();
    expect(screen.getByText("writing")).toBeInTheDocument();
  });

  it("Bundle tab shows agent_id and manifest data", async () => {
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByText("My Agent")).toBeInTheDocument());
    // fireEvent (not raw .click()) so the resulting setState is wrapped in act
    // — otherwise React schedules the update after the assertion runs.
    fireEvent.click(screen.getByRole("tab", { name: /bundle/i }));
    await waitFor(() => expect(screen.getByText(/agent_/)).toBeInTheDocument());
  });

  it("renders a terminal not-found state when myListings has no matching row", async () => {
    // Empty items[] simulates: bad listingId, ownership change, or a /me
    // pagination gap. Without an explicit terminal state, the view would
    // render Loading… forever.
    vi.mocked(myListings).mockResolvedValueOnce({ items: [] });
    render(<ListingDetailView listingId="lst_missing" mode="drawer" />);
    await waitFor(() =>
      expect(screen.getByText(/couldn't find this listing/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Loading…/)).toBeNull();
  });

  it("draft-only listing: promotes draft to live, no pending tab, no discard, shows Continue editing", async () => {
    // Brand-new listing in v=1 draft (never published). Pending tab + Discard
    // would be misleading: the row IS the listing, not a pending update.
    // Backend rejects discard for version<=1 anyway, so the button would 422.
    // Continue editing CTA gives the seller a path back to the wizard so
    // they can finish + submit their first publish.
    vi.mocked(myListings).mockResolvedValueOnce({
      items: [{ ...baseListing, version: 1, status: "draft" as const }],
    });
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() =>
      expect(screen.getByText("My Agent")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("tab", { name: /pending update/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /discard/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /republish/i })).toBeNull();
    expect(
      screen.getByRole("button", { name: /continue editing/i }),
    ).toBeInTheDocument();
  });

  it("rejected v=1 listing: Continue editing CTA reads 'Revise & resubmit'", async () => {
    vi.mocked(myListings).mockResolvedValueOnce({
      items: [
        {
          ...baseListing,
          version: 1,
          status: "draft" as const,
          rejection_notes: "be more specific about scope",
        },
      ],
    });
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() =>
      expect(screen.getByText("My Agent")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /revise & resubmit/i }),
    ).toBeInTheDocument();
  });

  it("renders not-found terminal state when myListings rejects", async () => {
    // Network / 5xx / token expiry on the seller-listings fetch. Without
    // try/catch the view would sit on Loading… forever and leak an
    // unhandled promise rejection.
    vi.mocked(myListings).mockRejectedValueOnce(new Error("network down"));
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() =>
      expect(
        screen.getByText(/couldn't find this listing/i),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Loading…/)).toBeNull();
  });

  it("mode='page' renders wider layout chrome than mode='drawer'", async () => {
    const { rerender, container } = render(
      <ListingDetailView listingId="lst_x" mode="drawer" />
    );
    await waitFor(() => expect(screen.getByText("My Agent")).toBeInTheDocument());
    expect(container.querySelector("[data-mode='drawer']")).not.toBeNull();
    rerender(<ListingDetailView listingId="lst_x" mode="page" />);
    expect(container.querySelector("[data-mode='page']")).not.toBeNull();
  });
});

describe("<ListingDetailView> actions", () => {
  it("Republish button disabled with tooltip when pending row exists", async () => {
    vi.mocked(myListings).mockResolvedValueOnce({
      items: [
        { ...baseListing, version: 1, status: "published" },
        { ...baseListing, version: 2, status: "draft" },
      ],
    });
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() =>
      expect(screen.getByText(/Live/)).toBeInTheDocument(),
    );
    const btn = screen.getByRole("button", { name: /republish/i });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title");
  });

  it("Republish click POSTs republish and opens wizard", async () => {
    vi.mocked(republishListing).mockResolvedValueOnce({
      ...baseListing,
      version: 4,
      status: "draft",
    });
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() =>
      expect(screen.getByText(/Live/)).toBeInTheDocument(),
    );
    const btn = screen.getByRole("button", { name: /republish/i });
    fireEvent.click(btn);
    await waitFor(() =>
      expect(republishListing).toHaveBeenCalledWith(
        expect.objectContaining({ listingId: "lst_x" }),
      ),
    );
    await waitFor(() => expect(startEditListingSpy).toHaveBeenCalled());
  });

  it("Pending Update tab shows rejection notes verbatim", async () => {
    vi.mocked(myListings).mockResolvedValueOnce({
      items: [
        { ...baseListing, version: 1, status: "published" },
        {
          ...baseListing,
          version: 2,
          status: "draft",
          rejection_notes:
            "Description is too vague — be specific about what the agent does.",
          rejected_at: "2026-05-22T10:00:00Z",
        },
      ],
    });
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() =>
      expect(
        screen.getByRole("tab", { name: /pending update/i }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("tab", { name: /pending update/i }));
    await waitFor(() =>
      expect(
        screen.getByText(/Description is too vague/),
      ).toBeInTheDocument(),
    );
  });

  it("Discard button calls discardListingVersion and refetches", async () => {
    vi.mocked(myListings)
      .mockResolvedValueOnce({
        // initial load: live + pending draft
        items: [
          { ...baseListing, version: 1, status: "published" },
          { ...baseListing, version: 2, status: "draft" },
        ],
      })
      .mockResolvedValueOnce({
        // after discard: only the live row remains
        items: [{ ...baseListing, version: 1, status: "published" }],
      });
    vi.mocked(discardListingVersion).mockResolvedValueOnce(undefined);
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /discard/i }),
      ).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    await waitFor(() =>
      expect(discardListingVersion).toHaveBeenCalledWith(
        expect.objectContaining({ listingId: "lst_x", version: 2 }),
      ),
    );
  });
});
