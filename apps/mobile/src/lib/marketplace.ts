/**
 * Marketplace buyer data layer (W5 contract §A).
 *
 * Bearer-only Clerk JWT everywhere (search/detail are anonymous-friendly on
 * the backend, but the mobile app is always signed in when it reaches these
 * screens). Follows the getTokenRef REST pattern from control/overview.tsx:
 * pin Clerk's churning `getToken` behind a ref so the memoized api client is
 * built exactly once and the fetch callbacks stay stable (a `[getToken]` dep
 * is an infinite-loop trap — avoided across the codebase, kept that way here).
 */
import { useAuth } from "@clerk/expo";
import { useCallback, useMemo, useRef, useState } from "react";
import {
  ApiError,
  createApiClient,
  type ApiMethods,
} from "@lightbulb/shared";

import { makeAdapter } from "./adapter";

// ---------------------------------------------------------------------------
// Contract shapes (w5-contract §A + web lib/marketplace/types.ts)
// ---------------------------------------------------------------------------

export type ListingFormat = "openclaw" | "skillmd";

/** Search-row + detail-row listing shape (subset the mobile buyer surfaces). */
export interface Listing {
  listing_id: string;
  slug: string;
  name: string;
  description_md: string;
  format: ListingFormat;
  /** Integer cents; 0 = free. */
  price_cents: number;
  tags: string[];
  category?: string | null;
  seller_id: string;
  /** "@handle" byline, resolved server-side; may be null. */
  seller_display_name?: string | null;
  status: string;
  version: number;
  published_at: string | null;
  created_at: string;
  install_count?: number;
}

/** A single classified dependency the agent needs (manifest-only). */
export interface RequirementRow {
  key: string;
  display_name: string;
  kind: string;
  disposition: "included" | "connect" | "byo" | "unavailable";
  /** Env-var NAMES only — never values. */
  env: string[];
  bundled: boolean;
  required: boolean;
  note?: string | null;
}

/** Detail manifest (from S3 manifest.json). Null on any S3 failure. */
export interface ListingManifest {
  name?: string;
  description?: string;
  emoji?: string;
  vibe?: string;
  suggested_model?: string;
  scan_incomplete?: boolean;
  requirements?: RequirementRow[];
}

export interface ListingDetail {
  listing: Listing;
  manifest: ListingManifest | null;
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

/** Free vs "$X.XX" from integer cents. */
export function formatPrice(priceCents: number): string {
  if (!priceCents || priceCents <= 0) return "FREE";
  return `$${(priceCents / 100).toFixed(2)}`;
}

/** First non-empty line of the markdown blurb, for a one-line summary. */
export function listingSummary(descriptionMd: string | null | undefined): string {
  if (!descriptionMd) return "";
  for (const raw of descriptionMd.split("\n")) {
    const line = raw.replace(/^#+\s*/, "").trim();
    if (line) return line;
  }
  return "";
}

/** "@handle" byline fallback chain. */
export function listingByline(listing: Pick<Listing, "seller_display_name">): string {
  const name = listing.seller_display_name?.trim();
  return name && name.length > 0 ? name : "Community member";
}

/**
 * Can this listing be hired from the phone? Mobile deploys FREE agents only —
 * a paid hire would mean initiating a purchase from inside the app, which is
 * the guideline 3.1.1 line #1643 pulled the whole marketplace back over. The
 * seller of a paid listing still deploys their own for free (backend
 * seller-self bypass), so their rows stay visible and hireable.
 *
 * The search list is filtered server-side by free_only, which applies this
 * same rule (free, or the caller's own) before the ranked limit slice. This
 * copy gates the detail screen's primary action, where a deep link can land
 * on a paid listing search never showed, and doubles as a belt over the rows
 * that do come back.
 *
 * ponytail: price + seller only. Neither payload carries an entitlement flag,
 * so a paid listing the viewer already BOUGHT (which deploys free) stays
 * hidden on mobile until one of them does.
 */
export function isFreeToHire(
  listing: Pick<Listing, "price_cents" | "seller_id">,
  userId: string | null | undefined,
): boolean {
  if (!listing.price_cents || listing.price_cents <= 0) return true;
  return userId != null && listing.seller_id === userId;
}

// ---------------------------------------------------------------------------
// Shared api-client hook (getTokenRef; built once)
// ---------------------------------------------------------------------------

function useMarketplaceClient(): ApiMethods {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const config = useMemo(() => makeAdapter(() => getTokenRef.current()), []);
  return useMemo(() => createApiClient(config), [config]);
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

export type SearchState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; items: Listing[] };

export function useMarketplaceSearch() {
  const api = useMarketplaceClient();
  const [state, setState] = useState<SearchState>({ kind: "loading" });
  const seqRef = useRef(0);

  const search = useCallback(
    async (query: string) => {
      const seq = ++seqRef.current;
      setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
      const q = query.trim();
      // free_only is applied server-side, BEFORE the ranked top-N slice.
      // Dropping paid rows from the returned page instead would silently
      // lose the free listings that ranked below the cut.
      const path = q
        ? `/marketplace/listings/search?free_only=true&q=${encodeURIComponent(q)}`
        : "/marketplace/listings/search?free_only=true";
      try {
        const body = (await api.get(path)) as { items?: Listing[] };
        if (seqRef.current !== seq) return;
        setState({ kind: "ready", items: body.items ?? [] });
      } catch (err) {
        if (seqRef.current !== seq) return;
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Request failed",
        });
      }
    },
    [api],
  );

  return { state, search };
}

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

export type DetailState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; detail: ListingDetail };

export function useListingDetail(listingId: string | undefined) {
  const api = useMarketplaceClient();
  const [state, setState] = useState<DetailState>({ kind: "loading" });
  const seqRef = useRef(0);

  const load = useCallback(async () => {
    if (!listingId) return;
    const seq = ++seqRef.current;
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      const body = (await api.get(
        `/marketplace/listings/${encodeURIComponent(listingId)}`,
      )) as ListingDetail;
      if (seqRef.current !== seq) return;
      setState({ kind: "ready", detail: body });
    } catch (err) {
      if (seqRef.current !== seq) return;
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : "Request failed",
      });
    }
  }, [api, listingId]);

  return { state, load };
}

// ---------------------------------------------------------------------------
// Deploy (free / owned / seller) — fully in-app
// ---------------------------------------------------------------------------

export interface DeployResult {
  agent_uuid: string;
  warnings?: unknown[];
}

/** Raised when the buyer lacks a provider the agent hard-requires (409). */
export class MissingCredentialsError extends Error {
  constructor(public readonly missing: string[]) {
    super("missing_credentials");
    this.name = "MissingCredentialsError";
  }
}

export function useDeployListing() {
  const api = useMarketplaceClient();
  return useCallback(
    async (listingId: string): Promise<DeployResult> => {
      try {
        const body = (await api.post(
          `/marketplace/listings/${encodeURIComponent(listingId)}/deploy`,
          {},
        )) as DeployResult;
        return body;
      } catch (err) {
        // 409 missing_credentials → the buyer must connect a provider first.
        if (err instanceof ApiError && err.status === 409) {
          const b = err.body as
            | { error?: string; missing?: string[] }
            | null;
          if (b?.error === "missing_credentials") {
            throw new MissingCredentialsError(b.missing ?? []);
          }
        }
        // Every other failure — including the backend's own paid-listing 403,
        // which the free-only client filter should have made unreachable —
        // surfaces with the server's detail string. Nothing routes to Stripe.
        throw err;
      }
    },
    [api],
  );
}

// No checkout hook lives here on purpose. Mobile hires free agents only; a
// paid hire is a web-app flow. Re-adding one puts a Stripe handoff back one
// import away from a screen.
