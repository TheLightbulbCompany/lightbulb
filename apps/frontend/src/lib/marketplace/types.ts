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
   * Public byline resolved server-side. When the seller has a marketplace
   * handle, this is `"@{handle}"`. Otherwise falls back to the Clerk-derived
   * name, then to `"Community member"`. Optional because older cached
   * responses may not include it yet.
   */
  seller_display_name?: string | null;
  status: ListingStatus;
  version: number;
  published_at: string | null;
  created_at: string;
  /** Lifetime deploy count. Written by the license service and returned on
   * published storefront/seller rows; absent on draft/retired rows. */
  install_count?: number;
  /** EFS agent id from the last artifact-from-agent call. Used by the
   * publish-wizard edit mode to lock the PickAgent step. Missing on
   * listings created before the column was added — fall back to
   * `manifest_json.agent_id`. */
  agent_id?: string | null;
  /** `requires_acp` rides in the artifact manifest (set by publishing with
   * enable_acp). Surfaced here so the publish wizard's edit mode can round-trip
   * the ACP toggle on revise-and-resubmit. */
  manifest_json?: { agent_id?: string; requires_acp?: boolean } & Record<string, unknown>;
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
 * A single external-dependency the agent needs, classified into a buyer-facing
 * disposition. Mirrors the backend `Requirement.to_manifest()` shape
 * (core/services/marketplace/requirements.py). Secret values never appear —
 * only env-var *names* in `env`.
 */
export interface RequirementRow {
  key: string;
  display_name: string;
  /** e.g. "skill" | "tool" | "mcp" | "cli" | "channel" | "external_api" */
  kind: string;
  disposition: "included" | "connect" | "byo" | "unavailable";
  /** Env-var names the buyer must supply, e.g. ["PARALLEL_API_KEY"]. Often empty. */
  env: string[];
  /** Source files the dependency was detected in. */
  detected_via: string[];
  bundled: boolean;
  required: boolean;
  note?: string | null;
  reason?: string | null;
}

/**
 * Manifest fields the storefront surfaces on the listing-detail page.
 * Sourced from <s3_prefix>manifest.json — the backend's GET /listings/{slug}
 * fetches that file and includes it as `manifest`. May be null if the S3
 * fetch failed; storefront falls back to listing-only rendering.
 *
 * The "What's bundled" section renders `requirements` — the classified
 * dependency ledger the publish pipeline writes
 * (`build_manifest_requirements`). When absent/empty the agent is
 * self-contained. `name`/`description`/`vibe`/`suggested_model`/`emoji` are
 * best-effort identity fields and may be missing on slimmer manifests.
 */
export interface ListingManifest {
  name?: string;
  description?: string;
  format?: ListingFormat;
  emoji?: string;
  vibe?: string;
  suggested_model?: string;
  /** Classified dependency ledger. Empty/undefined → self-contained agent. */
  requirements?: RequirementRow[];
  /** True when the scanner couldn't fully analyze the agent. */
  scan_incomplete?: boolean;
}

export interface ListingDetailResponse {
  listing: Listing;
  manifest: ListingManifest | null;
}
