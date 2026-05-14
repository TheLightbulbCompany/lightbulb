// Storefront-local types. Previously lived in packages/marketplace-shared
// but with the Isol8-internal scope cut there's only one consumer (this
// app), so inlining is simpler than carrying a workspace package.
//
// `format` keeps openclaw + skillmd for now — the field is on the
// listings table and may be useful for a future "publish a SKILL.md
// from your container" flow. v0 only emits openclaw.

export type ListingFormat = "openclaw" | "skillmd";
export type ListingStatus = "draft" | "review" | "published" | "retired" | "taken_down";

export interface Listing {
  listing_id: string;
  slug: string;
  name: string;
  description_md: string;
  format: ListingFormat;
  price_cents: number;
  tags: string[];
  seller_id: string;
  /**
   * Renderable seller handle, prefixed with `@` (e.g. `"@ledger"` or
   * `"@isol8"`). Annotated by the backend at the router boundary so
   * cards never have to display the raw `seller_id` (Clerk user_id).
   * Older payloads may omit this field; callers should fall back to
   * `"@isol8"` (the in-house default) rather than to `seller_id`.
   */
  seller_username?: string;
  status: ListingStatus;
  version: number;
  published_at: string | null;
  created_at: string;
}

export interface Purchase {
  purchase_id: string;
  listing_id: string;
  listing_slug: string;
  license_key: string;
  price_paid_cents: number;
  status: "paid" | "refunded" | "revoked";
  created_at: string;
}

/**
 * Manifest fields the storefront surfaces on the listing-detail page.
 * Sourced from <s3_prefix>manifest.json — the backend's GET /listings/{slug}
 * fetches that file and includes it as `manifest`. May be null if the S3
 * fetch failed; storefront falls back to listing-only rendering.
 *
 * Marketplace publish currently writes a slimmer manifest (just name +
 * description + format + exported_at + agent_id + file_count), so most of
 * the rich fields below will commonly be missing — the storefront treats
 * any missing field as "None / unknown".
 */
export interface ListingManifest {
  name?: string;
  description?: string;
  format?: ListingFormat;
  emoji?: string;
  vibe?: string;
  suggested_model?: string;
  required_skills?: string[];
  required_plugins?: string[];
  required_tools?: string[];
  suggested_channels?: string[];
}

export interface ListingDetailResponse {
  listing: Listing;
  manifest: ListingManifest | null;
}
