"use client";

import { useState } from "react";
import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import type { Listing } from "@/lib/marketplace/types";
import { useMarketplaceWriteGate } from "@/lib/marketplace/useMarketplaceWriteGate";
import { useHire } from "./useHire";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";
import { HireManagerDialog } from "./HireManagerDialog";

const FALLBACK_BYLINE = "Community member";

/**
 * Public byline for a marketplace listing.
 *
 * - When the listing belongs to the current Clerk user, render
 *   "you" (or "you (@handle)" once the seller has a handle set) so the
 *   self-attribution is obvious.
 * - Otherwise read the server-resolved `seller_display_name` — already
 *   `"@{handle}"` when the seller has a marketplace profile, falling back
 *   through Clerk identity to "Community member".
 */
function bylineFor(listing: Listing, currentUserId: string | null | undefined): string {
  const resolved = listing.seller_display_name || FALLBACK_BYLINE;
  if (currentUserId && listing.seller_id === currentUserId) {
    return resolved.startsWith("@") ? `you (${resolved})` : "you";
  }
  return resolved;
}

export function ListingCard({ listing, installed }: { listing: Listing; installed: boolean }) {
  const { hire, isHiring } = useHire();
  const { userId } = useAuth();
  const { restricted } = useMarketplaceWriteGate();
  const { openListingDetail } = useMarketplaceDrawer();
  const [showHire, setShowHire] = useState(false);
  const tagline =
    listing.description_md.split(/\n{2,}/, 1)[0]?.replace(/[#*_`]/g, "").trim() || "";
  const byline = bylineFor(listing, userId);
  // The seller never "hires" their own agent. Browse only surfaces published
  // listings, so an own listing here is live — send them to its detail page to
  // manage / republish instead of offering Hire.
  const isOwn = !!userId && listing.seller_id === userId;

  return (
    <article
      className={`relative rounded-2xl border p-5 flex flex-col gap-3 min-h-[200px] transition ${
        installed
          ? "border-[#aac6b4] bg-[#f4f8f4]"
          : "border-[#e0dbd0] bg-white hover:-translate-y-0.5 hover:shadow-md hover:border-[#cfc7b8]"
      }`}
    >
      {installed && (
        <span className="absolute top-3 right-3.5 text-[9.5px] tracking-widest text-[#06402b] font-bold">
          INSTALLED
        </span>
      )}
      <Link
        href={`/marketplace/listing/${listing.listing_id}`}
        target="_blank"
        rel="noopener noreferrer"
        className="font-editorial-serif text-lg leading-tight text-[#1a1a1a] no-underline hover:underline"
      >
        {listing.name}
      </Link>
      <p className="text-[13px] text-[#5b5b5b] leading-snug flex-1 line-clamp-2">{tagline}</p>
      <div className="flex items-center justify-between pt-2.5 border-t border-dashed border-[#e0dbd0]">
        <span className="text-[11.5px] italic text-[#5b5b5b]">by {byline}</span>
        {isOwn ? (
          <button
            type="button"
            data-testid="manage-listing"
            onClick={() => openListingDetail(listing.listing_id)}
            className="text-[#06402b] border border-[#06402b] bg-transparent px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold hover:bg-[#06402b] hover:text-white transition"
          >
            Manage
          </button>
        ) : installed ? (
          <button
            type="button"
            disabled
            className="text-[#06402b] border border-[#06402b] bg-transparent px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold opacity-70"
          >
            Hired ✓
          </button>
        ) : restricted ? (
          // Non-admin org members can't deploy. Backend POST /deploy 403s
          // for them; surface that upfront with a flat text affordance
          // instead of a button so there's nothing to click.
          <span
            data-testid="hire-restricted"
            className="text-[11.5px] italic text-[#8a8478]"
          >
            Ask your org admin to install
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setShowHire(true)}
            disabled={isHiring}
            className="bg-[#06402b] text-white px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold hover:bg-[#0a5a3d] disabled:opacity-60"
          >
            Hire
          </button>
        )}
      </div>
      {showHire && (
        <HireManagerDialog
          listing={listing}
          isHiring={isHiring}
          onConfirm={(reportsTo) => {
            // Keep the dialog open (its button shows "Hiring…") while the
            // deploy runs. On success the drawer closes and unmounts this; on
            // failure isHiring resets so the buyer can retry or cancel.
            void hire(listing, reportsTo);
          }}
          onClose={() => setShowHire(false)}
        />
      )}
    </article>
  );
}
