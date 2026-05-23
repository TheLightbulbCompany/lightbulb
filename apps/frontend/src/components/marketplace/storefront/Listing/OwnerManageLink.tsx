"use client";

import { useAuth } from "@clerk/nextjs";

/**
 * Renders a "Manage your listing →" link on the public listing detail page
 * when the signed-in viewer is the seller. Server-component parent passes
 * the seller_id + slug so we don't need an extra fetch — Clerk's userId is
 * the only thing that has to come from the client.
 *
 * Returns null in every non-owner case (signed-out, different user, Clerk
 * still loading) so the public layout stays untouched for everyone else.
 */
export function OwnerManageLink({
  sellerId,
  slug,
}: {
  sellerId: string;
  slug: string;
}) {
  const { userId } = useAuth();
  if (!userId || userId !== sellerId) return null;
  return (
    <a
      href={`/marketplace/listing/${slug}/manage`}
      className="mt-4 inline-block text-[12.5px] font-semibold text-[#06402b] hover:underline"
    >
      Manage your listing →
    </a>
  );
}
