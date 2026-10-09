import { Suspense } from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

import ManagePage from "../page";
import { myListings } from "@/lib/marketplace/api";

// Stable auth object — ManagePage's useEffect lists getToken as a dep, and a
// fresh function per render would refetch indefinitely (same trap that bit
// the ListingDetailView tests).
const { stableAuth } = vi.hoisted(() => ({
  stableAuth: {
    isLoaded: true,
    userId: "seller_1" as string | null,
    getToken: async () => "test-jwt",
  },
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => stableAuth,
}));

vi.mock("@/lib/marketplace/api", () => ({
  // Both the page's owner gate and ListingDetailView read the seller's own
  // listings; stubbed so neither hits the network.
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
function resolvedParams(listingId: string) {
  return {
    status: "fulfilled" as const,
    value: { listingId },
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
        params={
          resolvedParams("lst_x") as unknown as Promise<{ listingId: string }>
        }
      />
    </Suspense>,
  );
  // Flush ManagePage's useEffect microtask chain — myListings resolves,
  // then setListing/setForbidden + setListing's render.
  await act(async () => {});
  return result;
}

beforeEach(() => {
  vi.mocked(myListings).mockReset();
  vi.mocked(myListings).mockResolvedValue({ items: [baseListing] });
  startEditListingSpy.mockReset();
});

describe("/marketplace/listing/[listingId]/manage", () => {
  it("renders ListingDetailView for the owner", async () => {
    await renderManagePage();
    // ListingDetailView re-fetches via myListings → live row → name visible.
    await waitFor(() =>
      expect(screen.getByText("Agent X")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /republish/i }),
    ).toBeInTheDocument();
  });

  it("opens the owner's own draft, offers Delete, and hides the dead public link", async () => {
    // The public GET 404s for drafts, so the page used to bounce the owner
    // to a public page that 404s too.
    vi.mocked(myListings).mockResolvedValue({
      items: [{ ...baseListing, status: "draft" as const, published_at: null }],
    });
    await renderManagePage();
    await waitFor(() => expect(screen.getByText("Agent X")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /delete draft/i })).toBeInTheDocument();
    expect(screen.queryByText(/view on marketplace/i)).toBeNull();
  });

  it("redirects non-owner to public listing page", async () => {
    // /listings/search?seller_id=<me> never returns someone else's listing.
    vi.mocked(myListings).mockResolvedValue({ items: [] });

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
      expect(replaceSpy).toHaveBeenCalledWith("/marketplace/listing/lst_x"),
    );

    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: originalLocation,
    });
  });

  it("soft-redirects when the listings fetch rejects (401/network)", async () => {
    // Token-expiry / transient failure must terminate the gate flow —
    // otherwise the page sits on indefinite Loading… and the owner-gate
    // effect never completes.
    vi.mocked(myListings).mockRejectedValueOnce(new Error("offline"));

    const replaceSpy = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { ...originalLocation, replace: replaceSpy },
    });

    await renderManagePage();
    await waitFor(() =>
      expect(replaceSpy).toHaveBeenCalledWith("/marketplace/listing/lst_x"),
    );

    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: originalLocation,
    });
  });

  it("redirects unauthenticated visitors to public listing page", async () => {
    // Clerk has resolved but there's no signed-in user — auth.isLoaded is
    // true with userId=null. Without the redirect, the visitor would sit
    // on an indefinite Loading… screen.
    stableAuth.userId = null;

    const replaceSpy = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { ...originalLocation, replace: replaceSpy },
    });

    await renderManagePage();
    await waitFor(() =>
      expect(replaceSpy).toHaveBeenCalledWith("/marketplace/listing/lst_x"),
    );
    // No authed fetch — we shortcut before it.
    expect(myListings).not.toHaveBeenCalled();

    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: originalLocation,
    });
    stableAuth.userId = "seller_1";
  });
});
