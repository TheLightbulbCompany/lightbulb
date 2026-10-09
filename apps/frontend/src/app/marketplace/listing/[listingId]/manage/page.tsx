"use client";

import { use, useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import Link from "next/link";

import { myListings } from "@/lib/marketplace/api";
import { ListingDetailView } from "@/components/marketplace/ListingDetailView";
import { MarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawer";
import { MarketplaceDrawerProvider } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import type { Listing } from "@/lib/marketplace/types";

/**
 * Owner-gated full-page surface for managing a published listing —
 * Republish, Discard, and the Pending-Update review live here. A non-owner
 * who hits this URL is redirected to the public listing page; we never
 * 404 the page so the URL stays shareable for sellers.
 *
 * The marketplace layout doesn't mount MarketplaceDrawerProvider (only
 * /chat does), so we wrap locally — ListingDetailView's Header + PendingTab
 * call useMarketplaceDrawer().startEditListing to hand off to the publish
 * wizard, which lives behind that context.
 */
export default function ManagePage({
  params,
}: {
  params: Promise<{ listingId: string }>;
}) {
  // Next.js 16 makes route params async; unwrap with React.use() in client
  // components — `await params` would force the file into a server component.
  const { listingId } = use(params);
  const { isLoaded, userId, getToken } = useAuth();
  const [listing, setListing] = useState<Listing | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    if (!isLoaded) return; // Clerk still resolving; wait for the next render.
    if (!userId) {
      // Auth resolved but no signed-in user. Send them to the public listing
      // page rather than stranding them on an indefinite Loading… screen.
      // Synchronizing Clerk's resolved auth into view state is the
      // external-system→React sync the rule allows.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setForbidden(true);
      window.location.replace(`/marketplace/listing/${listingId}`);
      return;
    }
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!alive || !jwt) return;
      // The seller's OWN listings, not the public GET: that one only serves
      // published rows, so a draft or in-review listing 404'd and bounced its
      // owner to a public page that 404s too.
      let row: Listing | undefined;
      try {
        const { items } = await myListings({ sellerId: userId, jwt });
        row = items.find((l) => l.listing_id === listingId);
      } catch {
        // 404 (bad listing_id), 401 (token expired), 5xx, network. All map to
        // the same UX: send them to the public listing page rather than
        // stranding on indefinite Loading…. The public page handles 404
        // with its own NotFound state.
        if (!alive) return;
        setForbidden(true);
        window.location.replace(`/marketplace/listing/${listingId}`);
        return;
      }
      if (!alive) return;
      if (!row || row.seller_id !== userId) {
        // Soft-redirect rather than throwing — owners arriving via a stale
        // session or admins poking the URL should land on the public page,
        // not a hard error.
        setForbidden(true);
        window.location.replace(`/marketplace/listing/${listingId}`);
        return;
      }
      setListing(row);
    })();
    return () => {
      alive = false;
    };
  }, [listingId, isLoaded, userId, getToken]);

  if (forbidden) return null;
  if (!listing) return <p className="p-8">Loading…</p>;

  return (
    <MarketplaceDrawerProvider>
      <main className="min-h-screen bg-background">
        <nav className="border-b border-[var(--edge)] px-6 py-3">
          <Link
            href="/marketplace"
            className="text-[12.5px] font-semibold text-[var(--midnight)] hover:underline"
          >
            ← Marketplace
          </Link>
        </nav>
        <ListingDetailView listingId={listing.listing_id} mode="page" />
      </main>
      {/* MarketplaceDrawer renders the publish wizard when startEditListing
          fires (Republish / Continue editing). Without it, clicks succeed
          but no UI appears. */}
      <MarketplaceDrawer />
    </MarketplaceDrawerProvider>
  );
}
