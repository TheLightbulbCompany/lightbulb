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
import { useAuth } from "@clerk/clerk-expo";
import { useCallback, useMemo, useRef, useState } from "react";
import {
  ApiError,
  createApiClient,
  type ApiMethods,
} from "@isol8/shared";

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
      const path = q
        ? `/marketplace/listings/search?q=${encodeURIComponent(q)}`
        : "/marketplace/listings/search";
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

/**
 * Raised when the deploy 403s specifically because the caller is not entitled
 * to a PAID listing (no purchase row, and not the seller) — backend detail
 * "purchase required to deploy this listing" (marketplace_listings.py:393).
 *
 * This is the ONLY 403 that should route the buyer to Stripe checkout. The
 * other deploy 403 — "Organization admin access required" (require_org_admin)
 * — must NOT trigger checkout, because checkout runs the same admin guard and
 * would 403 identically; it surfaces as a plain error instead.
 */
export class PurchaseRequiredError extends Error {
  constructor() {
    super("purchase_required");
    this.name = "PurchaseRequiredError";
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
        // 403 with the purchase-required detail → not entitled to a paid
        // listing; distinguish from the org-admin 403 by the detail string.
        if (err instanceof ApiError && err.status === 403) {
          const detail = (err.body as { detail?: unknown } | null)?.detail;
          if (
            typeof detail === "string" &&
            detail.toLowerCase().includes("purchase required")
          ) {
            throw new PurchaseRequiredError();
          }
        }
        throw err;
      }
    },
    [api],
  );
}

// ---------------------------------------------------------------------------
// Checkout (paid) — D1: NO in-app Stripe. Returns a Stripe-hosted URL the
// caller opens in the SYSTEM BROWSER via Linking.openURL.
// ---------------------------------------------------------------------------

const WEB_ORIGIN = "https://isol8.co";

export function useCheckoutListing() {
  const api = useMarketplaceClient();
  return useCallback(
    async (listingId: string): Promise<string> => {
      const body = (await api.post("/marketplace/checkout", {
        listing_id: listingId,
        // Stripe substitutes {CHECKOUT_SESSION_ID}; the web /deploy-success
        // page then fires the authenticated deploy after the webhook grants
        // the entitlement.
        success_url: `${WEB_ORIGIN}/deploy-success?listing_id=${encodeURIComponent(
          listingId,
        )}&session_id={CHECKOUT_SESSION_ID}`,
        cancel_url: `${WEB_ORIGIN}/chat`,
      })) as { checkout_url?: string };
      if (!body.checkout_url) {
        throw new Error("No checkout URL returned");
      }
      return body.checkout_url;
    },
    [api],
  );
}
