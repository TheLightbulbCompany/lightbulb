"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";

/**
 * The one-click hire card.
 *
 * Rendered from the `recommend_agents` tool result. The agent that produced it
 * has no ability to hire — this button is the approval gate, and it deploys
 * through the ordinary authenticated marketplace route carrying the viewer's
 * own session. Nothing agent-side ever mutates the roster.
 */

export interface HireListing {
  /** Canonical identity. Slugs are NOT unique, so deploy addresses this. */
  listing_id: string;
  slug: string;
  name: string;
  summary: string;
  tags: string[];
  price_cents: number;
}

export interface HireSuggestion {
  kind: "isol8.hire_suggestion";
  reason: string;
  listings: HireListing[];
}

function asListing(value: unknown): HireListing | null {
  if (!value || typeof value !== "object") return null;
  const l = value as Record<string, unknown>;
  if (typeof l.slug !== "string" || !l.slug) return null;
  if (typeof l.listing_id !== "string" || !l.listing_id) return null;
  return {
    listing_id: l.listing_id,
    slug: l.slug,
    name: typeof l.name === "string" ? l.name : l.slug,
    summary: typeof l.summary === "string" ? l.summary : "",
    tags: Array.isArray(l.tags) ? l.tags.filter((t): t is string => typeof t === "string") : [],
    price_cents: typeof l.price_cents === "number" ? l.price_cents : 0,
  };
}

/**
 * Type guard over an untrusted tool result.
 *
 * Returns null for anything that is not a well-formed hire suggestion, so every
 * other tool falls through to the ordinary tool pill unchanged. The value comes
 * across a process boundary from the container, so nothing about it is assumed.
 */
export function parseHireSuggestion(value: unknown): HireSuggestion | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (v.kind !== "isol8.hire_suggestion") return null;
  if (!Array.isArray(v.listings)) return null;
  const listings = v.listings.map(asListing).filter((l): l is HireListing => l !== null);
  if (listings.length === 0) return null;
  return {
    kind: "isol8.hire_suggestion",
    reason: typeof v.reason === "string" ? v.reason : "",
    listings,
  };
}

function priceLabel(cents: number): string {
  return cents === 0 ? "Free" : `$${(cents / 100).toFixed(2)}`;
}

function ListingRow({ listing }: { listing: HireListing }) {
  const api = useApi();
  const [state, setState] = useState<"idle" | "deploying" | "done">("idle");
  const [error, setError] = useState<string | null>(null);

  async function deploy() {
    setState("deploying");
    setError(null);
    try {
      // By listing_id, never slug: slugs are non-unique, so a slug-addressed
      // deploy would 404 or resolve the wrong listing.
      await api.post(`/marketplace/listings/${listing.listing_id}/deploy`, {});
      setState("done");
    } catch (e) {
      // Surfaced in place rather than swallowed: a listing can be retired
      // between the recommendation and the click, and the user needs to know
      // the agent was not added.
      setError(e instanceof Error ? e.message : "Deploy failed");
      setState("idle");
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="font-medium">{listing.name}</div>
          {listing.summary && (
            <div className="text-sm text-muted-foreground">{listing.summary}</div>
          )}
          {listing.tags.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {listing.tags.map((tag) => (
                <Badge key={tag} variant="secondary" className="text-xs">
                  {tag}
                </Badge>
              ))}
            </div>
          )}
        </div>
        <span className="shrink-0 text-xs text-muted-foreground">
          {priceLabel(listing.price_cents)}
        </span>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" onClick={deploy} disabled={state !== "idle"}>
          {state === "deploying"
            ? "Deploying…"
            : state === "done"
              ? "Added to your team"
              : "Deploy to my team"}
        </Button>
        {error && <span className="text-xs text-destructive">{error}</span>}
      </div>
    </div>
  );
}

export function HireCard({ suggestion }: { suggestion: unknown }) {
  const parsed = parseHireSuggestion(suggestion);
  if (!parsed) return null;
  return (
    <div className="my-2 space-y-2">
      {parsed.reason && <div className="text-sm text-muted-foreground">{parsed.reason}</div>}
      {parsed.listings.map((listing) => (
        <ListingRow key={listing.listing_id} listing={listing} />
      ))}
    </div>
  );
}
