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
  category?: string | null;
  seller_id: string;
  /**
   * Human-readable name for the byline. Resolved server-side from the
   * seller's Clerk profile (curator → "isol8", others → first+last or
   * email local-part, else "Community member"). Optional because older
   * cached responses may not have it yet.
   */
  seller_display_name?: string | null;
  status: ListingStatus;
  version: number;
  published_at: string | null;
  created_at: string;
  /** EFS agent id from the last artifact-from-agent call. Used by the
   * publish-wizard edit mode to lock the PickAgent step. Missing on
   * listings created before the column was added — fall back to
   * `manifest_json.agent_id`. */
  agent_id?: string | null;
  manifest_json?: { agent_id?: string } & Record<string, unknown>;
  /** Last seen bundle selection. Persisted alongside the artifact so the
   * edit-mode BundleStep can prefill checkboxes — without these, a
   * revise-and-resubmit would silently re-export with `bundle: {}` and
   * strip integrations the listing previously shipped. */
  bundle_mcps?: string[] | null;
  bundle_skills?: string[] | null;
  /** Admin moderation feedback. Present iff the listing was rejected and
   * has not yet been resubmitted (reject flips status back to draft and
   * sets these). */
  rejection_notes?: string | null;
  rejected_at?: string | null;
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
