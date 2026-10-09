"use client";

import { useEffect, useState } from "react";
import { Search } from "lucide-react";

import { searchListings } from "@/lib/marketplace/api";
import { useCatalog } from "@/hooks/useCatalog";
import type { Listing } from "@/lib/marketplace/types";
import { CompanyListingCard } from "./CompanyListingCard";
import { ListingCard } from "./ListingCard";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";

const CATEGORIES = ["all", "sales", "engineering", "operations", "research", "finance"] as const;

export function BrowseView() {
  const [items, setItems] = useState<Listing[]>([]);
  const [q, setQ] = useState("");
  const [category, setCategory] = useState<string>("all");
  // Browse scope: single agents (the default hire flow) or whole pre-built
  // companies. Companies have no categories, so the category pills hide.
  const [scope, setScope] = useState<"agents" | "companies">("agents");
  const [loadError, setLoadError] = useState(false);
  const [loading, setLoading] = useState(true);
  const { installedSlugs } = useCatalog();
  // Optimistic overlay: slugs hired this session, before the catalog cache
  // reflects them. Unioned with the catalog set so a just-hired card shows
  // "Hired ✓" instantly and can't be re-hired into a duplicate.
  const { state } = useMarketplaceDrawer();

  useEffect(() => {
    let alive = true;
    const handle = setTimeout(() => {
      if (!alive) return;
      setLoading(true);
      setLoadError(false);
      searchListings({
        q: q || undefined,
        category: scope === "companies" || category === "all" ? undefined : category,
        format: scope === "companies" ? "company" : undefined,
      })
        .then((r) => {
          if (!alive) return;
          setItems(r.items);
          setLoading(false);
        })
        .catch(() => {
          // A failed search must not render as an empty catalog — "No agents
          // on the marketplace yet" on a network error reads as a wiped marketplace.
          if (alive) {
            setLoadError(true);
            setLoading(false);
          }
        });
    }, 300);
    return () => {
      alive = false;
      clearTimeout(handle);
    };
  }, [q, category, scope]);

  return (
    <div>
      <div className="flex gap-2.5 mb-4 items-center flex-wrap">
        <div className="flex-1 min-w-[220px] flex items-center gap-2.5 bg-cream-deep border border-[var(--edge)] rounded-xl px-3.5 py-2.5">
          <Search className="w-4 h-4 text-muted-foreground" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={
              scope === "companies" ? "Search companies…" : "Search agents, skills, or tags…"
            }
            className="flex-1 outline-0 bg-transparent text-sm"
          />
        </div>
        <div className="flex rounded-full border border-[var(--edge)] p-0.5" role="tablist">
          {(["agents", "companies"] as const).map((s) => (
            <button
              key={s}
              type="button"
              role="tab"
              aria-selected={scope === s}
              onClick={() => {
                if (s === scope) return;
                setScope(s);
                // Clear synchronously: until the debounced refetch lands, the
                // grid would otherwise render the OLD scope's rows through the
                // NEW scope's card — company rows briefly get a single-agent
                // "Hire" button, and agent rows render as nothing at all.
                setItems([]);
                setLoading(true);
              }}
              className={`px-3 py-1.5 rounded-full text-ui ${
                scope === s ? "bg-[var(--midnight)] text-[var(--cream)]" : "bg-transparent text-muted-foreground"
              }`}
            >
              {s === "agents" ? "Agents" : "Companies"}
            </button>
          ))}
        </div>
      </div>
      {scope === "agents" && (
        <div className="flex gap-2 flex-wrap mb-4">
          {CATEGORIES.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setCategory(c)}
              className={`px-3 py-1.5 rounded-full text-ui border ${
                category === c
                  ? "bg-[var(--midnight)] text-[var(--cream)] border-[var(--midnight)]"
                  : "bg-transparent text-muted-foreground border-[var(--edge)]"
              }`}
            >
              {c[0].toUpperCase() + c.slice(1)}
            </button>
          ))}
        </div>
      )}
      <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(240px,1fr))]">
        {loading && items.length === 0 ? (
          <p className="font-editorial-serif italic text-[var(--midnight-muted)] col-span-full">Loading…</p>
        ) : loadError ? (
          <p className="font-editorial-serif italic text-[var(--midnight-muted)] col-span-full" role="alert">
            Couldn&apos;t load the marketplace — check your connection and try another search.
          </p>
        ) : items.length === 0 ? (
          <p className="font-editorial-serif italic text-[var(--midnight-muted)] col-span-full">
            {q.trim() || (scope === "agents" && category !== "all")
              ? `No ${scope} match that search.`
              : `No ${scope} on the marketplace yet.`}
          </p>
        ) : scope === "companies" ? (
          items.map((listing) => (
            <CompanyListingCard key={listing.listing_id} listing={listing} />
          ))
        ) : (
          items.map((listing) => (
            <ListingCard
              key={listing.listing_id}
              listing={listing}
              installed={
                (installedSlugs?.has?.(listing.slug) ?? false) ||
                state.installedSlugs.has(listing.slug)
              }
            />
          ))
        )}
      </div>
    </div>
  );
}
