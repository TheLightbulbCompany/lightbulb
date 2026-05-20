import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockSearch = vi.fn();
vi.mock("@/lib/marketplace/api", () => ({
  searchListings: (opts: unknown) => mockSearch(opts),
}));
vi.mock("@/hooks/useCatalog", () => ({
  useCatalog: () => ({ installedSlugs: new Set(["pulse"]), deploy: vi.fn() }),
}));

// useHire (called from ListingCard) reaches into useAgents -> useGateway. The
// test renders ListingCards without a GatewayProvider, so we mock useAgents
// here to avoid the throw. Same root cause as PR #567 P1: anything inside the
// drawer that uses useHire needs gateway context (real or mocked).
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [], refresh: vi.fn().mockResolvedValue(undefined) }),
}));

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  useMarketplaceDrawer: () => ({
    state: { freshlyInstalledAgentId: null, toast: null },
    open: vi.fn(),
    close: vi.fn(),
    setView: vi.fn(),
    showInstallToast: vi.fn(),
    dismissToast: vi.fn(),
    setFreshlyInstalled: vi.fn(),
  }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ getToken: vi.fn().mockResolvedValue("tok_test") }),
  useUser: () => ({ user: { primaryEmailAddress: { emailAddress: "buyer@example.com" } } }),
  // ListingCard now uses the admin gate. null membership = personal user → gate open.
  useOrganization: () => ({ membership: null }),
}));

import { BrowseView } from "@/components/marketplace/drawer/BrowseView";

const SAMPLE = [
  {
    listing_id: "l1", slug: "atlas", name: "Atlas", description_md: "Engineering helper",
    seller_id: "u_curator", price_cents: 0, status: "published", format: "openclaw",
    tags: ["engineering"], version: 1, created_at: "2026-05-01", published_at: "2026-05-01",
  },
  {
    listing_id: "l2", slug: "pulse", name: "Pulse", description_md: "Sales digest",
    seller_id: "u_curator", price_cents: 1200, status: "published", format: "openclaw",
    tags: ["sales"], version: 1, created_at: "2026-05-01", published_at: "2026-05-01",
  },
];

describe("BrowseView", () => {
  beforeEach(() => mockSearch.mockReset());

  it("renders cards from searchListings response", async () => {
    mockSearch.mockResolvedValueOnce({ items: SAMPLE });
    render(<BrowseView />);
    await waitFor(() => expect(screen.getByText("Atlas")).toBeInTheDocument());
    expect(screen.getByText("Pulse")).toBeInTheDocument();
  });

  it("already-installed listings show 'Hired ✓' instead of Hire", async () => {
    mockSearch.mockResolvedValueOnce({ items: SAMPLE });
    render(<BrowseView />);
    await waitFor(() => expect(screen.getByText("Pulse")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /hired ✓/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^hire$/i })).toBeVisible();
  });

  it("category chip click refetches with the category param", async () => {
    mockSearch.mockResolvedValue({ items: [] });
    const user = userEvent.setup();
    render(<BrowseView />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: /sales/i }));
    await waitFor(() => expect(mockSearch).toHaveBeenCalledTimes(2));
    expect(mockSearch).toHaveBeenLastCalledWith(
      expect.objectContaining({ category: "sales" }),
    );
  });

  it("renders six category chips per spec", async () => {
    mockSearch.mockResolvedValueOnce({ items: [] });
    render(<BrowseView />);
    await waitFor(() => expect(mockSearch).toHaveBeenCalled());
    ["all", "sales", "engineering", "operations", "research", "finance"].forEach((c) => {
      expect(screen.getByRole("button", { name: new RegExp(`^${c}$`, "i") })).toBeVisible();
    });
  });
});
