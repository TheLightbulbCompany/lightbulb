import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MarketplaceTeaser } from "../MarketplaceTeaser";
import { browseListings } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

vi.mock("@/lib/marketplace/api", () => ({ browseListings: vi.fn() }));

const mockedBrowse = vi.mocked(browseListings);

function listing(overrides: Partial<Listing> = {}): Listing {
  return {
    listing_id: "lst_1",
    slug: "sourcing-scout",
    name: "Sourcing Scout",
    description_md: "# Sourcing Scout\n\nFinds **suppliers** and negotiates quotes.",
    format: "openclaw",
    price_cents: 1500,
    tags: ["sourcing", "ops", "supply", "extra"],
    seller_id: "user_1",
    status: "published",
    version: 1,
    published_at: "2026-08-01T00:00:00Z",
    created_at: "2026-07-01T00:00:00Z",
    install_count: 3,
    ...overrides,
  };
}

const STATIC_SIX = ["Scout", "Quill", "Halo", "Ledger", "Beacon", "Pace"];

describe("MarketplaceTeaser", () => {
  beforeEach(() => {
    mockedBrowse.mockReset();
  });

  it("renders live listings: name, description snippet, tags, price, hires", async () => {
    mockedBrowse.mockResolvedValue({ items: [listing()], count: 1 });
    render(await MarketplaceTeaser());
    expect(screen.getByText("Sourcing Scout")).toBeInTheDocument();
    // Markdown is stripped down to a plain snippet.
    expect(
      screen.getByText(/Finds suppliers and negotiates quotes/),
    ).toBeInTheDocument();
    expect(screen.getByText("sourcing")).toBeInTheDocument();
    // Tags cap at three.
    expect(screen.queryByText("extra")).not.toBeInTheDocument();
    expect(screen.getByText("$15")).toBeInTheDocument();
    expect(screen.getByText("3 hires")).toBeInTheDocument();
    // The browse link survives.
    expect(
      screen.getByRole("link", { name: /browse the marketplace/i }),
    ).toHaveAttribute("href", "/marketplace");
  });

  it("preserves cents on non-whole-dollar prices", async () => {
    mockedBrowse.mockResolvedValue({
      items: [
        listing({ listing_id: "lst_2", name: "Cents", price_cents: 1499 }),
        listing({ listing_id: "lst_3", name: "Pennies", price_cents: 49 }),
      ],
      count: 2,
    });
    render(await MarketplaceTeaser());
    expect(screen.getByText("$14.99")).toBeInTheDocument();
    expect(screen.getByText("$0.49")).toBeInTheDocument();
  });

  it("falls back to the static six when the API errors", async () => {
    mockedBrowse.mockRejectedValue(new Error("api down"));
    render(await MarketplaceTeaser());
    for (const name of STATIC_SIX) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
  });

  it("falls back to the static six when the API returns no listings", async () => {
    mockedBrowse.mockResolvedValue({ items: [], count: 0 });
    render(await MarketplaceTeaser());
    for (const name of STATIC_SIX) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
  });
});
