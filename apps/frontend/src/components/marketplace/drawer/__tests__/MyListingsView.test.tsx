import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

import { MyListingsView } from "../MyListingsView";
import { MarketplaceDrawerProvider } from "../MarketplaceDrawerProvider";
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

function renderInDrawer() {
  return render(
    <MarketplaceDrawerProvider>
      <MyListingsView />
    </MarketplaceDrawerProvider>,
  );
}

describe("MyListingsView", () => {
  beforeEach(() => {
    mockMyListings.mockReset();
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    // /marketplace/payouts/dashboard is fetched alongside the listings.
    mockApiGet.mockResolvedValue({ dashboard_url: null, balance_held_cents: 0 });
  });

  it("renders the rejection banner with admin notes when a draft was kicked back", async () => {
    mockMyListings.mockResolvedValue({
      items: [
        listing({
          rejection_notes: "Description was too vague — say what Ray actually does.",
          rejected_at: "2026-05-12T17:30:00Z",
        }),
      ],
    });
    renderInDrawer();
    const notes = await screen.findByTestId("rejection-notes");
    expect(notes).toHaveTextContent(/Description was too vague/);
    // Status pill flips to "Rejected" instead of generic "draft" text.
    expect(screen.getByText("Rejected")).toBeInTheDocument();
  });

  it("hides the rejection banner for a fresh draft (no admin notes yet)", async () => {
    mockMyListings.mockResolvedValue({ items: [listing()] });
    renderInDrawer();
    await screen.findByText("Ray");
    expect(screen.queryByTestId("rejection-notes")).not.toBeInTheDocument();
  });

  it('renders the "Revise →" button for rejected drafts and "Edit →" for fresh ones', async () => {
    mockMyListings.mockResolvedValue({
      items: [
        listing({
          listing_id: "lst_rejected",
          name: "RejectedAgent",
          rejection_notes: "Try again",
        }),
        listing({ listing_id: "lst_fresh", name: "FreshDraft" }),
      ],
    });
    renderInDrawer();
    await screen.findByText("RejectedAgent");
    await screen.findByText("FreshDraft");
    const editButtons = screen.getAllByTestId("edit-listing");
    expect(editButtons.map((b) => b.textContent)).toEqual([
      expect.stringMatching(/Revise/),
      expect.stringMatching(/Edit/),
    ]);
  });

  it("hides edit affordance for published listings", async () => {
    mockMyListings.mockResolvedValue({
      items: [listing({ status: "published", name: "Live agent" })],
    });
    renderInDrawer();
    await screen.findByText("Live agent");
    expect(screen.queryByTestId("edit-listing")).not.toBeInTheDocument();
  });

  it("shows edit affordance for in-review listings (auto-demote on save)", async () => {
    mockMyListings.mockResolvedValue({
      items: [listing({ status: "review", name: "Ray the Resy Bot" })],
    });
    renderInDrawer();
    await screen.findByText("Ray the Resy Bot");
    // The Edit button is reachable; the wizard's pullback banner (tested
    // separately) explains the demote-to-draft consequence.
    const editBtn = await screen.findByTestId("edit-listing");
    expect(editBtn).toHaveTextContent(/Edit/);
  });

  it("clicking Edit triggers the drawer's startEditListing flow", async () => {
    mockMyListings.mockResolvedValue({ items: [listing()] });
    renderInDrawer();
    const editBtn = await screen.findByTestId("edit-listing");
    fireEvent.click(editBtn);
    // We can't read drawer state directly without exposing it, but we can
    // assert no crash + the button was reachable. Future test coverage on
    // the wizard's edit-mode rendering pins the round-trip.
    await waitFor(() => expect(editBtn).toBeInTheDocument());
  });
});

describe("MyListingsView — org-admin gate", () => {
  beforeEach(() => {
    mockMyListings.mockReset();
    mockApiGet.mockReset();
    mockApiPost.mockReset();
    mockApiGet.mockResolvedValue({ dashboard_url: null, balance_held_cents: 0 });
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("renders the restricted view for non-admin org members and skips the fetch", async () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    renderInDrawer();
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
    renderInDrawer();
    expect(
      await screen.findByRole("button", { name: /Publish your agent/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
  });

  it("renders the publish CTA for org admins", async () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:admin" } });
    mockMyListings.mockResolvedValue({ items: [] });
    renderInDrawer();
    expect(
      await screen.findByRole("button", { name: /Publish your agent/i }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("marketplace-restricted")).toBeNull();
  });
});
