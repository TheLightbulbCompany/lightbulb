"use client";

import { CompanyCard } from "@/components/marketplace/storefront/Listing/CompanyCard";
import type { Listing } from "@/lib/marketplace/types";

export function CompaniesBrowse({ items }: { items: Listing[] }) {
  return (
    <section className="mx-auto max-w-6xl px-6 py-10">
      {items.length === 0 ? (
        <p className="py-20 text-center font-editorial-serif text-2xl italic text-[var(--midnight-muted)]">
          No companies listed yet.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
          {items.map((listing) => (
            <CompanyCard key={listing.listing_id} listing={listing} />
          ))}
        </div>
      )}
    </section>
  );
}
