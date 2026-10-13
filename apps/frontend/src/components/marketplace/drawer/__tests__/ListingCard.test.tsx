import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { ListingCard } from "../ListingCard";
import type { Listing } from "@/lib/marketplace/types";

// next/link is fine to render as a passthrough — we don't navigate.
vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

// Default: personal user (no org). Individual tests override `mockUseOrganization`.
const mockUseOrganization = vi.fn(() => ({ membership: null as null | { role: string } }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "u_viewer", isSignedIn: true, getToken: vi.fn() }),
  useUser: () => ({ user: { id: "u_viewer", primaryEmailAddress: null } }),
  useOrganization: () => ({ isLoaded: true, ...mockUseOrganization() }),
}));

// useHire pulls in useCatalog + useAgents + MarketplaceDrawerProvider — mock
// the whole hook so we don't have to mount the drawer for a leaf-component test.
const mockHire = vi.fn();
vi.mock("../useHire", () => ({
  useHire: () => ({ hire: mockHire, isHiring: false }),
}));

// ListingCard reads openListingDetail from the drawer context for the "Manage"
// (own-listing) branch — mock it so the leaf component renders without a provider.
const mockOpenListingDetail = vi.fn();
vi.mock("../MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({ openListingDetail: mockOpenListingDetail }),
}));

// The hire dialog reads the roster to populate its "reports to" picker.
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: () => ({ data: { agents: [] }, isLoading: false, error: null }),
  }),
}));

function listing(overrides: Partial<Listing> = {}): Listing {
  return {
    listing_id: "lst_atlas",
    slug: "atlas",
    name: "Atlas",
    description_md: "Clears your inbox before standup.",
    format: "openclaw",
    price_cents: 0,
    tags: [],
    seller_id: "u_other_seller",
    status: "published",
    version: 1,
    published_at: null,
    created_at: "2026-05-10T12:00:00Z",
    ...overrides,
  };
}

describe("Drawer ListingCard — org-admin gate", () => {
  beforeEach(() => {
    mockHire.mockReset();
    mockOpenListingDetail.mockReset();
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("renders the Hire button for personal users (no membership)", () => {
    render(<ListingCard listing={listing()} installed={false} />);
    expect(screen.getByRole("button", { name: /Hire/i })).toBeInTheDocument();
    expect(screen.queryByTestId("hire-restricted")).toBeNull();
  });

  it("renders the Hire button for org admins", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:admin" } });
    render(<ListingCard listing={listing()} installed={false} />);
    expect(screen.getByRole("button", { name: /Hire/i })).toBeInTheDocument();
    expect(screen.queryByTestId("hire-restricted")).toBeNull();
  });

  it("hides the Hire button for non-admin org members and shows ask-admin text", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    render(<ListingCard listing={listing()} installed={false} />);
    expect(screen.queryByRole("button", { name: /Hire/i })).toBeNull();
    expect(screen.getByTestId("hire-restricted")).toHaveTextContent(
      /Ask your org admin/i,
    );
  });

  it("does not show ask-admin text on already-installed cards (Hired badge wins)", () => {
    mockUseOrganization.mockReturnValue({ membership: { role: "org:member" } });
    render(<ListingCard listing={listing()} installed={true} />);
    // The installed-state branch renders before the restricted branch in JSX,
    // so the "Hired ✓" badge wins. That's intentional — an already-installed
    // listing reflects past entitlement, not a current write attempt.
    expect(screen.getByRole("button", { name: /Hired/i })).toBeInTheDocument();
    expect(screen.queryByTestId("hire-restricted")).toBeNull();
  });
});

describe("Drawer ListingCard — own-listing (Manage)", () => {
  beforeEach(() => {
    mockHire.mockReset();
    mockOpenListingDetail.mockReset();
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("shows Manage (not Hire) for the viewer's own listing and opens detail", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    render(<ListingCard listing={listing({ seller_id: "u_viewer" })} installed={false} />);
    const manage = screen.getByTestId("manage-listing");
    expect(manage).toHaveTextContent(/Manage/i);
    expect(screen.queryByRole("button", { name: /^Hire$/i })).toBeNull();
    await userEvent.click(manage);
    expect(mockOpenListingDetail).toHaveBeenCalledWith("lst_atlas");
    expect(mockHire).not.toHaveBeenCalled();
  });

  it("shows Manage for an own listing even when installed (own beats Hired)", () => {
    render(<ListingCard listing={listing({ seller_id: "u_viewer" })} installed={true} />);
    expect(screen.getByTestId("manage-listing")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Hired/i })).toBeNull();
  });

  it("still shows Hire for another seller's listing", () => {
    render(<ListingCard listing={listing({ seller_id: "u_other_seller" })} installed={false} />);
    expect(screen.getByRole("button", { name: /^Hire$/i })).toBeInTheDocument();
    expect(screen.queryByTestId("manage-listing")).toBeNull();
  });
});

describe("Drawer ListingCard — hire dialog placement", () => {
  beforeEach(() => {
    mockHire.mockReset();
    mockUseOrganization.mockReturnValue({ membership: null });
  });

  it("renders the hire dialog outside the card, not as its descendant", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    const { container } = render(<ListingCard listing={listing()} installed={false} />);
    await userEvent.click(screen.getByRole("button", { name: /^Hire$/i }));

    const dialog = screen.getByRole("dialog");
    // The card carries `hover:-translate-y-0.5`, and a non-none translate makes
    // it a containing block for position:fixed descendants — in-tree, the
    // full-screen overlay collapsed into the card (clipped, half-width) and
    // flickered, because the overlay itself kept the card hovered. The dialog
    // must stay portalled out of the card.
    expect(container.querySelector("article")?.contains(dialog)).toBe(false);
  });
});
