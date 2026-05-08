"use client";

import { useEffect, useState } from "react";
import { Search } from "lucide-react";

import { searchListings } from "@/lib/marketplace/api";
import { useCatalog } from "@/hooks/useCatalog";
import type { Listing } from "@/lib/marketplace/types";
import { ListingCard } from "./ListingCard";

const CATEGORIES = ["all", "sales", "engineering", "operations", "research", "finance"] as const;

export function BrowseView() {
  const [items, setItems] = useState<Listing[]>([]);
  const [q, setQ] = useState("");
  const [category, setCategory] = useState<string>("all");
  const [loading, setLoading] = useState(true);
  const { installedSlugs } = useCatalog() as unknown as { installedSlugs: Set<string> };

  useEffect(() => {
    let alive = true;
    const handle = setTimeout(() => {
      if (!alive) return;
      setLoading(true);
      searchListings({
        q: q || undefined,
        category: category === "all" ? undefined : category,
      })
        .then((r) => {
          if (!alive) return;
          setItems(r.items);
          setLoading(false);
        })
        .catch(() => {
          if (alive) setLoading(false);
        });
    }, 300);
    return () => {
      alive = false;
      clearTimeout(handle);
    };
  }, [q, category]);

  return (
    <div>
      <div className="flex gap-2.5 mb-4 items-center flex-wrap">
        <div className="flex-1 min-w-[220px] flex items-center gap-2.5 bg-white border border-[#e0dbd0] rounded-xl px-3.5 py-2.5">
          <Search className="w-4 h-4 text-[#5b5b5b]" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search agents, skills, or tags…"
            className="flex-1 outline-0 bg-transparent text-sm"
          />
        </div>
        <button className="bg-white border border-[#e0dbd0] rounded-xl px-3.5 py-2.5 text-[13px] text-[#5b5b5b]">
          Most installed ▾
        </button>
      </div>
      <div className="flex gap-2 flex-wrap mb-4">
        {CATEGORIES.map((c) => (
          <button
            key={c}
            type="button"
            onClick={() => setCategory(c)}
            className={`px-3 py-1.5 rounded-full text-[12.5px] border ${
              category === c
                ? "bg-[#06402b] text-white border-[#06402b]"
                : "bg-transparent text-[#5b5b5b] border-[#e0dbd0]"
            }`}
          >
            {c[0].toUpperCase() + c.slice(1)}
          </button>
        ))}
      </div>
      <div className="grid gap-4 grid-cols-[repeat(auto-fill,minmax(240px,1fr))]">
        {loading && items.length === 0 ? (
          <p className="font-editorial-serif italic text-[#8a8578] col-span-full">Loading…</p>
        ) : items.length === 0 ? (
          <p className="font-editorial-serif italic text-[#8a8578] col-span-full">
            No agents fit that brief.
          </p>
        ) : (
          items.map((listing) => (
            <ListingCard
              key={listing.listing_id}
              listing={listing}
              installed={installedSlugs?.has?.(listing.slug) ?? false}
            />
          ))
        )}
      </div>
    </div>
  );
}
