"use client";

import { useState } from "react";
import { ChevronRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * The one-click hire card.
 *
 * Rendered from the `recommend_agents` tool result. The agent that produced it
 * has no ability to hire — the Deploy button is the approval gate, and it
 * deploys through the ordinary authenticated marketplace route carrying the
 * viewer's own session. Nothing agent-side ever mutates the roster.
 *
 * Shape: one Recommended card (the only one with a Deploy button) plus the
 * rest of Atlas's suggestions collapsed behind an "N alternatives" drawer, one
 * line each. Clicking an alternative PROMOTES it to Recommended — it only
 * swaps which listing is primary in local state; it is not, and must never
 * become, a deploy trigger. Five equal cards with five equal buttons told the
 * user nothing about which agent Atlas actually thought they wanted; this
 * keeps the roster mutation gated behind exactly one button.
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

/** The primary card — full detail, and the only place Deploy appears. */
function PrimaryListing({ listing }: { listing: HireListing }) {
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
    <article aria-label={listing.name} className="rounded-lg border border-border bg-card p-3">
      <div className="mb-1.5 text-2xs font-medium uppercase tracking-caps text-[var(--midnight-muted)]">
        Recommended
      </div>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold">{listing.name}</div>
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
    </article>
  );
}

/**
 * One alternative, collapsed to a single line. No Deploy button — the whole
 * row is a promote action, and promoting only changes local `primaryId`
 * state. It never calls the API, so browsing alternatives can never be
 * mistaken for hiring one.
 */
function AlternativeRow({ listing, onPromote }: { listing: HireListing; onPromote: () => void }) {
  return (
    <li>
      <article aria-label={listing.name}>
        <button
          type="button"
          onClick={onPromote}
          aria-label={`Make ${listing.name} the recommendation`}
          className="chat-hire-alt-btn group flex w-full items-baseline justify-between gap-2.5 py-1.5 text-left"
        >
          <span className="min-w-0 truncate text-ui leading-snug">
            <span className="font-medium text-[var(--midnight-soft)] group-hover:text-[var(--midnight)]">
              {listing.name}
            </span>
            {listing.summary && (
              <span className="text-[var(--midnight-muted)]"> — {listing.summary}</span>
            )}
          </span>
          <span className="shrink-0 text-caption tabular-nums text-[var(--midnight-muted)]">
            {priceLabel(listing.price_cents)}
          </span>
        </button>
      </article>
    </li>
  );
}

export function HireCard({ suggestion }: { suggestion: unknown }) {
  const parsed = parseHireSuggestion(suggestion);
  // Which listing_id is primary. Null until the user promotes one, which
  // means "Atlas's own first pick" — listings[] arrives in Atlas's ranked
  // order, so index 0 is the recommendation until overridden locally.
  const [primaryId, setPrimaryId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  if (!parsed) return null;

  const primary = parsed.listings.find((l) => l.listing_id === primaryId) ?? parsed.listings[0]!;
  const alternatives = parsed.listings.filter((l) => l.listing_id !== primary.listing_id);

  function promote(listing: HireListing) {
    setPrimaryId(listing.listing_id);
    setOpen(false);
  }

  return (
    <div className="my-2 space-y-2">
      {parsed.reason && <div className="text-sm text-muted-foreground">{parsed.reason}</div>}
      <PrimaryListing key={primary.listing_id} listing={primary} />
      {alternatives.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="inline-flex items-center gap-1.5 text-ui text-[var(--midnight-muted)] transition-colors hover:text-[var(--midnight-soft)]"
          >
            <ChevronRight
              aria-hidden
              className={cn("h-3 w-3 shrink-0 text-[var(--midnight-faint)] transition-transform", open && "rotate-90")}
            />
            {alternatives.length} alternative{alternatives.length === 1 ? "" : "s"}
          </button>
          {open && (
            <>
              <ul className="chat-hire-alt-list mt-2 list-none space-y-1 border-l border-[var(--edge)] pl-[13px]">
                {alternatives.map((listing) => (
                  <AlternativeRow
                    key={listing.listing_id}
                    listing={listing}
                    onPromote={() => promote(listing)}
                  />
                ))}
              </ul>
              {/* --midnight-faint is non-text-only (3.37:1 on cream), so this
                  reuses --midnight-muted rather than the mock's literal token. */}
              <div className="mt-1.5 text-caption italic text-[var(--midnight-muted)]">
                Picking one promotes it to Recommended. It does not deploy.
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
