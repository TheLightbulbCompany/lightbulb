"use server";

import { adminGet, adminPost, type ActionResult } from "./_shared";

/**
 * Server Actions for marketplace moderation (admin dashboard).
 *
 * Wraps the backend `/admin/marketplace/*` endpoints so the moderation UI
 * can list / approve / reject / take down listings and read the takedowns
 * audit log.
 *
 * Takedowns workflow note: under the Isol8-internal scope there is no
 * public DMCA filing form, so the takedown queue is structurally empty.
 * The admin initiates the takedown directly from the listing detail page;
 * the takedowns route is reframed as an audit-log view of historical
 * takedowns (`listRecentTakedowns`).
 */

/**
 * List marketplace listings for the admin browse view.
 *
 * Backwards-compatible: called with no params, returns the moderation
 * review queue (status="review" rows) — same shape the page used to get
 * from the now-renamed `listReviewQueue`.
 *
 * With params, filters by status and/or whitespace-tokenized search query.
 * `status` is one of "draft" | "review" | "published" | "retired" |
 * "taken_down". The backend clamps `limit` to 200; passing higher values
 * is a no-op rather than an error.
 */
export async function listListings(params?: {
  status?: string;
  q?: string;
  limit?: number;
}): Promise<ActionResult> {
  const search = new URLSearchParams();
  if (params?.status) search.set("status", params.status);
  if (params?.q) search.set("q", params.q);
  if (params?.limit !== undefined) search.set("limit", String(params.limit));
  const qs = search.toString();
  return adminGet(`/admin/marketplace/listings${qs ? `?${qs}` : ""}`);
}

/**
 * Approve a pending listing so it becomes visible in the marketplace.
 *
 * `version` MUST be passed through from the review row (defaults to 1 only
 * for safety on legacy callers). The backend defaults `version=1`, so a
 * v2+ moderation action without this would target the wrong row and 409.
 *
 * `prevVersion` triggers the publish_v2 atomic flip on backend (retires
 * the previous published version + publishes the new one in one
 * TransactWriteItems). Pass it for any version > 1.
 */
export async function approveListing(
  listingId: string,
  options: { version?: number; prevVersion?: number } = {},
): Promise<ActionResult> {
  const { version = 1, prevVersion } = options;
  const params = new URLSearchParams({ version: String(version) });
  if (prevVersion !== undefined) params.set("prev_version", String(prevVersion));
  return adminPost(
    `/admin/marketplace/listings/${encodeURIComponent(listingId)}/approve?${params.toString()}`,
  );
}

/**
 * Reject a pending listing. `notes` is surfaced to the publisher and recorded
 * on the moderation audit log.
 *
 * `version` MUST be passed through from the review row (see approveListing).
 */
export async function rejectListing(
  listingId: string,
  notes: string,
  options: { version?: number } = {},
): Promise<ActionResult> {
  const { version = 1 } = options;
  const params = new URLSearchParams({ version: String(version) });
  return adminPost(
    `/admin/marketplace/listings/${encodeURIComponent(listingId)}/reject?${params.toString()}`,
    { notes },
  );
}

/** Listing detail preview for moderators (file tree, content, safety scan).
 *
 * Pass ``version`` to pin the exact row — needed from the browse view
 * because a listing_id can have multiple versions (e.g., published v1 +
 * review v2) and the backend's default heuristic would pick the review
 * row, hiding the published version the moderator clicked on.
 */
export async function getListingPreview(
  listingId: string,
  options: { version?: number } = {},
): Promise<ActionResult> {
  const qs = options.version !== undefined ? `?version=${options.version}` : "";
  return adminGet(
    `/admin/marketplace/listings/${encodeURIComponent(listingId)}/preview${qs}`,
  );
}

/** List recent takedowns for the audit-log view (newest first). */
export async function listRecentTakedowns(): Promise<ActionResult> {
  return adminGet("/admin/marketplace/takedowns");
}

/**
 * Admin-initiated takedown against a listing.
 *
 * Writes the takedown row + cascades license revocation + flips the
 * listing's status to `taken_down` in one shot. `reason` is the structured
 * category for routing and analytics; `basisMd` is the free-text reason
 * recorded on the audit log (and shown to anyone reviewing the takedown
 * later).
 */
export async function takedownListing(
  listingId: string,
  reason: "dmca" | "policy" | "fraud" | "seller-request",
  basisMd: string,
): Promise<ActionResult> {
  return adminPost(
    `/admin/marketplace/listings/${encodeURIComponent(listingId)}/takedown`,
    { reason, basis_md: basisMd },
  );
}

