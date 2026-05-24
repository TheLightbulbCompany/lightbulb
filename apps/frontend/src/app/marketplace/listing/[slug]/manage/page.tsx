"use client";

import { use, useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";

import { getListingBySlug } from "@/lib/marketplace/api";
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
  params: Promise<{ slug: string }>;
}) {
  // Next.js 16 makes route params async; unwrap with React.use() in client
  // components — `await params` would force the file into a server component.
  const { slug } = use(params);
  const { isLoaded, userId, getToken } = useAuth();
  const [listing, setListing] = useState<Listing | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    if (!isLoaded) return; // Clerk still resolving; wait for the next render.
    if (!userId) {
      // Auth resolved but no signed-in user. Send them to the public listing
      // page rather than stranding them on an indefinite Loading… screen.
      setForbidden(true);
      window.location.replace(`/marketplace/listing/${slug}`);
      return;
    }
    let alive = true;
    (async () => {
      const jwt = await getToken();
      if (!alive || !jwt) return;
      const row = await getListingBySlug({ slug, jwt });
      if (!alive) return;
      if (row.seller_id !== userId) {
        // Soft-redirect rather than throwing — owners arriving via a stale
        // session or admins poking the URL should land on the public page,
        // not a hard error.
        setForbidden(true);
        window.location.replace(`/marketplace/listing/${slug}`);
        return;
      }
      setListing(row);
    })();
    return () => {
      alive = false;
    };
  }, [slug, isLoaded, userId, getToken]);

  if (forbidden) return null;
  if (!listing) return <p className="p-8">Loading…</p>;

  return (
    <MarketplaceDrawerProvider>
      <main className="min-h-screen bg-[#fbf9f4]">
        <nav className="border-b border-[#e0dbd0] px-6 py-3">
          <a
            href="/marketplace"
            className="text-[12.5px] font-semibold text-[#06402b] hover:underline"
          >
            ← Marketplace
          </a>
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
