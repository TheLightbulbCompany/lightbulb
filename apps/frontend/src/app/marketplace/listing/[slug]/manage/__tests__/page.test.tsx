import { Suspense } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

import ManagePage from "../page";
import { getListingBySlug, myListings } from "@/lib/marketplace/api";

// Stable auth object — ManagePage's useEffect lists getToken as a dep, and a
// fresh function per render would refetch indefinitely (same trap that bit
// the ListingDetailView tests).
const { stableAuth } = vi.hoisted(() => ({
  stableAuth: { userId: "seller_1", getToken: async () => "test-jwt" },
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => stableAuth,
}));

vi.mock("@/lib/marketplace/api", () => ({
  getListingBySlug: vi.fn(),
  // ListingDetailView calls myListings under the hood; the page test stubs it
  // so the inner component can resolve without hitting the network.
  myListings: vi.fn().mockResolvedValue({ items: [] }),
  republishListing: vi.fn(),
  discardListingVersion: vi.fn(),
}));

// MarketplaceDrawer pulls in heavy chat-stack dependencies (Clerk session
// hooks, drawer state, etc.) — stub it so the page test stays focused on
// ManagePage's owner-gate + ListingDetailView wiring.
vi.mock("@/components/marketplace/drawer/MarketplaceDrawer", () => ({
  MarketplaceDrawer: () => null,
}));

// Hoisted spy so we can both inject into the mocked provider and assert
// against it later. vi.mock runs before module init, so a plain const
// declared at module scope would still be undefined when the mock factory
// is evaluated.
const { startEditListingSpy } = vi.hoisted(() => ({
  startEditListingSpy: vi.fn(),
}));

vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({
  // The real provider is bypassed in tests — useMarketplaceDrawer just
  // returns enough surface for ListingDetailView's Header + PendingTab.
  MarketplaceDrawerProvider: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
  useMarketplaceDrawer: () => ({
    startEditListing: startEditListingSpy,
    setView: vi.fn(),
  }),
}));

const baseListing = {
  listing_id: "lst_x",
  version: 1,
  status: "published" as const,
  name: "Agent X",
  description_md: "d",
  slug: "x",
  seller_id: "seller_1",
  price_cents: 500,
  tags: [] as string[],
  category: "agent",
  format: "openclaw" as const,
  created_at: "2026-05-23T00:00:00Z",
  published_at: "2026-05-23T00:00:00Z",
};

// React.use() suspends on any Promise — even one that's already resolved —
// because internally it always awaits one microtask. To skip the suspension
// in tests, we hand it a "thenable" with the React internal status fields
// pre-set, which React.use() reads synchronously and returns immediately.
// See React's `trackUsedThenable` for the contract.
function resolvedParams(slug: string) {
  return {
    status: "fulfilled" as const,
    value: { slug },
    then() {},
  };
}

// Test wrapper — keeps a Suspense boundary as a safety net but the
// resolvedParams thenable above means React.use() resolves on the initial
// render, so the fallback should never render in practice.
async function renderManagePage() {
  const result = render(
    <Suspense fallback={<div data-testid="suspense-fallback" />}>
      <ManagePage
        params={resolvedParams("x") as unknown as Promise<{ slug: string }>}
      />
    </Suspense>,
  );
  // Flush ManagePage's useEffect microtask chain — getListingBySlug resolves,
  // then setListing/setForbidden + setListing's render.
  await act(async () => {});
  return result;
}

beforeEach(() => {
  vi.mocked(getListingBySlug).mockReset();
  vi.mocked(myListings).mockReset();
  vi.mocked(myListings).mockResolvedValue({ items: [baseListing] });
  startEditListingSpy.mockReset();
});

describe("/marketplace/listing/[slug]/manage", () => {
  it("renders ListingDetailView for the owner", async () => {
    vi.mocked(getListingBySlug).mockResolvedValueOnce(baseListing);
    await renderManagePage();
    // ListingDetailView re-fetches via myListings → live row → name visible.
    await waitFor(() =>
      expect(screen.getByText("Agent X")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /republish/i }),
    ).toBeInTheDocument();
  });

  it("redirects non-owner to public listing page", async () => {
    const otherListing = { ...baseListing, seller_id: "someone_else" };
    vi.mocked(getListingBySlug).mockResolvedValueOnce(otherListing);

    // jsdom's window.location.replace is read-only by default — stub the
    // whole location object so the redirect is observable.
    const replaceSpy = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { ...originalLocation, replace: replaceSpy },
    });

    await renderManagePage();
    await waitFor(() =>
      expect(replaceSpy).toHaveBeenCalledWith("/marketplace/listing/x"),
    );

    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: originalLocation,
    });
  });
});
