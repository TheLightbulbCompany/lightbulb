import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

import { MarketplaceSection } from "@/components/landing/MarketplaceSection";
import type { Listing } from "@/lib/marketplace/types";

const mockFeatured = vi.fn();

vi.mock("@/lib/marketplace/api", () => ({
  featuredListings: (opts: { limit?: number }) => mockFeatured(opts),
}));

function makeListing(over: Partial<Listing> = {}): Listing {
  return {
    listing_id: "l_1",
    slug: "pulse",
    name: "Pulse",
    description_md: "Marketing intel and ops, autonomously.\n\nMore.",
    format: "openclaw",
    price_cents: 0,
    tags: ["marketing", "ops"],
    seller_id: "user_curator",
    status: "published",
    version: 1,
    published_at: "2026-05-01T00:00:00Z",
    created_at: "2026-05-01T00:00:00Z",
    ...over,
  };
}

describe("MarketplaceSection", () => {
  beforeEach(() => {
    mockFeatured.mockReset();
  });

  it("renders the section header + browse CTA even when the API returns no items", async () => {
    mockFeatured.mockResolvedValueOnce({ items: [] });
    const ui = await MarketplaceSection();
    const { container } = render(ui);
    expect(container.querySelector(".section-eyebrow")?.textContent).toMatch(/the marketplace/i);
    expect(screen.getByRole("link", { name: /browse the marketplace/i })).toHaveAttribute(
      "href",
      "/marketplace",
    );
    // No card grid when items is empty
    expect(screen.queryByText("Pulse")).toBeNull();
  });

  it("renders curator listings as linkable cards", async () => {
    mockFeatured.mockResolvedValueOnce({
      items: [
        makeListing(),
        makeListing({ listing_id: "l_2", slug: "atlas", name: "Atlas", price_cents: 1200 }),
      ],
    });
    const ui = await MarketplaceSection();
    render(ui);
    const pulse = screen.getByRole("link", { name: /pulse/i });
    expect(pulse).toHaveAttribute("href", "/marketplace/listing/pulse");
    const atlas = screen.getByRole("link", { name: /atlas/i });
    expect(atlas).toHaveAttribute("href", "/marketplace/listing/atlas");
    // Free / Premium tier label, no dollar amount
    expect(screen.getByText("Free")).toBeInTheDocument();
    expect(screen.getByText("Premium")).toBeInTheDocument();
    expect(screen.queryByText(/\$/)).toBeNull();
  });

  it("falls back to the section shell when the API throws", async () => {
    mockFeatured.mockRejectedValueOnce(new Error("network"));
    const ui = await MarketplaceSection();
    render(ui);
    expect(screen.getByRole("link", { name: /browse the marketplace/i })).toBeInTheDocument();
  });
});
