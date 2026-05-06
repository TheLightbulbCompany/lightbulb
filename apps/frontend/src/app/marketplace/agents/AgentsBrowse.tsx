"use client";

import { useMemo, useState } from "react";

import { ListingCard } from "@/components/marketplace/storefront/Listing/ListingCard";
import type { Listing } from "@/lib/marketplace/types";

export function AgentsBrowse({ items }: { items: Listing[] }) {
  const [activeTag, setActiveTag] = useState<string>("all");

  const tags = useMemo(() => {
    const seen = new Set<string>();
    for (const listing of items) {
      for (const tag of listing.tags) seen.add(tag);
    }
    return Array.from(seen).sort();
  }, [items]);

  const filtered = useMemo(() => {
    if (activeTag === "all") return items;
    return items.filter((listing) => listing.tags.includes(activeTag));
  }, [items, activeTag]);

  return (
    <>
      <section className="sticky top-[64px] z-10 border-y border-[var(--edge)] bg-[rgba(250,247,242,0.92)] backdrop-blur-xl">
        <div className="mx-auto flex max-w-6xl items-center gap-2 overflow-x-auto px-6 py-4">
          <TagPill label="All" count={items.length} active={activeTag === "all"} onClick={() => setActiveTag("all")} />
          {tags.map((tag) => (
            <TagPill
              key={tag}
              label={tag}
              count={items.filter((listing) => listing.tags.includes(tag)).length}
              active={activeTag === tag}
              onClick={() => setActiveTag(tag)}
            />
          ))}
        </div>
      </section>

      <section className="mx-auto max-w-6xl px-6 py-10">
        {filtered.length === 0 ? (
          <p className="py-20 text-center font-editorial-serif text-2xl italic text-[var(--mute)]">
            No agents fit that brief.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
            {filtered.map((listing) => (
              <ListingCard key={listing.listing_id} listing={listing} />
            ))}
          </div>
        )}
      </section>
    </>
  );
}

function TagPill({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`shrink-0 rounded-full border px-4 py-1.5 text-sm font-medium transition ${
        active
          ? "border-[var(--forest)] bg-[var(--forest)] text-[var(--cream)]"
          : "border-[var(--edge)] text-[var(--ink-soft)] hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
      }`}
    >
      <span>{label}</span>
      <span className="ml-1.5 font-mono text-[10px] opacity-70">{count}</span>
    </button>
  );
}
