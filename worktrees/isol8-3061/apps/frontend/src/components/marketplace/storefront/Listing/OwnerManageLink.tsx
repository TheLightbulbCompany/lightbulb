"use client";

import { useAuth } from "@clerk/nextjs";

/**
 * Renders a "Manage your listing →" link on the public listing detail page
 * when the signed-in viewer is the seller. Server-component parent passes
 * the seller_id + listing_id so we don't need an extra fetch — Clerk's userId
 * is the only thing that has to come from the client.
 *
 * Returns null in every non-owner case (signed-out, different user, Clerk
 * still loading) so the public layout stays untouched for everyone else.
 */
export function OwnerManageLink({
  sellerId,
  listingId,
}: {
  sellerId: string;
  listingId: string;
}) {
  const { userId } = useAuth();
  if (!userId || userId !== sellerId) return null;
  return (
    <a
      href={`/marketplace/listing/${listingId}/manage`}
      className="mt-4 inline-block text-ui font-semibold text-[var(--midnight)] hover:underline"
    >
      Manage your listing →
    </a>
  );
}
