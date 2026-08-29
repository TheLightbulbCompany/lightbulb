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
      className={`relative rounded-xl border p-5 flex flex-col gap-3 min-h-[200px] transition ${
        installed
          ? "border-[var(--ok)]"
          : "border-[var(--edge)] bg-card hover:shadow-md hover:border-[var(--midnight-faint)] [@media(hover:hover)_and_(pointer:fine)]:hover:-translate-y-0.5"
      }`}
    >
      {installed && (
        <span className="absolute top-3 right-3.5 text-2xs tracking-caps text-[var(--midnight)] font-bold">
          INSTALLED
        </span>
      )}
      <Link
        href={`/marketplace/listing/${listing.listing_id}`}
        target="_blank"
        rel="noopener noreferrer"
        className="font-editorial-serif text-lg leading-tight text-[var(--midnight)] no-underline hover:underline"
      >
        {listing.name}
      </Link>
      {/* No `flex-1` here: stretching the box past two lines defeats
          line-clamp — the ellipsis still lands at line 2 while lines 3+ stay
          visible ("across chat, email, SMS, an… voice."). The footer's
          `mt-auto` does the same spacing job without inflating this box. */}
      <p className="text-ui text-muted-foreground leading-snug line-clamp-2">{tagline}</p>
      <div className="mt-auto flex items-center justify-between pt-2.5 border-t border-dashed border-[var(--edge)]">
        <span className="text-caption italic text-muted-foreground">by {byline}</span>
        {isOwn ? (
          <button
            type="button"
            data-testid="manage-listing"
            onClick={() => openListingDetail(listing.listing_id)}
            className="text-[var(--midnight)] border border-[var(--midnight)] bg-transparent px-3.5 py-1.5 rounded-full text-ui font-semibold hover:bg-[var(--midnight)] hover:text-[var(--cream)] transition"
          >
            Manage
          </button>
        ) : installed ? (
          <button
            type="button"
            disabled
            className="text-[var(--midnight)] border border-[var(--midnight)] bg-transparent px-3.5 py-1.5 rounded-full text-ui font-semibold opacity-70"
          >
            Hired ✓
          </button>
        ) : restricted ? (
          // Non-admin org members can't deploy. Backend POST /deploy 403s
          // for them; surface that upfront with a flat text affordance
          // instead of a button so there's nothing to click.
          <span
            data-testid="hire-restricted"
            className="text-caption italic text-muted-foreground"
          >
            Ask your org admin to install
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setShowHire(true)}
            disabled={isHiring}
            className="bg-[var(--midnight)] text-[var(--cream)] px-3.5 py-1.5 rounded-full text-ui font-semibold hover:bg-[var(--midnight-soft)] disabled:opacity-50"
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
