import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { ListingDetailView } from "../ListingDetailView";

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "seller_1", getToken: async () => "test-jwt" }),
}));
const baseListing = {
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
};

vi.mock("@/lib/marketplace/api", () => ({
  getListingBySlug: vi.fn().mockResolvedValue(baseListing),
  // ListingDetailView fetches the seller's full list and filters by listing_id.
  // The detail row(s) must appear in items[] for the component to resolve.
  myListings: vi.fn().mockResolvedValue({ items: [baseListing] }),
}));

describe("<ListingDetailView>", () => {
  it("renders live row metadata in Overview tab", async () => {
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByText("My Agent")).toBeInTheDocument());
    expect(screen.getByText(/Live/)).toBeInTheDocument();
    expect(screen.getByText(/17 installs/)).toBeInTheDocument();
    expect(screen.getByText("productivity")).toBeInTheDocument();
    expect(screen.getByText("writing")).toBeInTheDocument();
  });

  it("Bundle tab shows agent_id and manifest data", async () => {
    render(<ListingDetailView listingId="lst_x" mode="drawer" />);
    await waitFor(() => expect(screen.getByText("My Agent")).toBeInTheDocument());
    // Click Bundle tab
    screen.getByRole("tab", { name: /bundle/i }).click();
    await waitFor(() => expect(screen.getByText(/agent_/)).toBeInTheDocument());
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
