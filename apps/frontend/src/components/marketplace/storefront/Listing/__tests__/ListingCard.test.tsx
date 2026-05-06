import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ListingCard } from "../ListingCard";
import type { Listing } from "@/lib/marketplace/types";

vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

const baseListing: Listing = {
  listing_id: "l1",
  slug: "atlas",
  name: "Atlas",
  description_md: "Clears your inbox before standup. Drafts, defers, dispatches.",
  format: "openclaw",
  price_cents: 0,
  tags: ["communication", "ops"],
  seller_id: "s1",
  status: "published",
  version: 1,
  published_at: null,
  created_at: "2026-05-01",
};

describe("ListingCard", () => {
  it("links to the listing detail page", () => {
    render(<ListingCard listing={baseListing} />);
    expect(screen.getByRole("link")).toHaveAttribute("href", "/marketplace/listing/atlas");
  });

  it("shows 'Free' for zero-price listings", () => {
    render(<ListingCard listing={baseListing} />);
    expect(screen.getByText("Free")).toBeInTheDocument();
  });

  it("shows the dollar amount for paid listings", () => {
    render(<ListingCard listing={{ ...baseListing, price_cents: 2500 }} />);
    expect(screen.getByText("$25.00")).toBeInTheDocument();
  });

  it("renders the first sentence of the description as the italic vibe", () => {
    render(<ListingCard listing={baseListing} />);
    expect(screen.getByText(/Clears your inbox before standup/)).toBeInTheDocument();
  });

  it("renders up to 3 tag pills (excess tags hidden)", () => {
    render(
      <ListingCard
        listing={{ ...baseListing, tags: ["alpha", "bravo", "charlie", "delta"] }}
      />,
    );
    expect(screen.getByText("bravo")).toBeInTheDocument();
    expect(screen.getByText("charlie")).toBeInTheDocument();
    expect(screen.queryByText("delta")).not.toBeInTheDocument();
  });
});
