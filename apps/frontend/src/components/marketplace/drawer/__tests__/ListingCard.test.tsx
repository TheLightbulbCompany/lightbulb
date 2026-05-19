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
  useOrganization: () => mockUseOrganization(),
}));

// useHire pulls in useCatalog + useAgents + MarketplaceDrawerProvider — mock
// the whole hook so we don't have to mount the drawer for a leaf-component test.
const mockHire = vi.fn();
vi.mock("../useHire", () => ({
  useHire: () => ({ hire: mockHire, isHiring: false }),
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
