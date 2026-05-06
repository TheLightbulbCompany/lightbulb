import Link from "next/link";

import { Monogram, AgentPill } from "@/components/agents/AgentIdentity";
import { fromListing } from "@/lib/agentDesign";
import type { Listing } from "@/lib/marketplace/types";

export function ListingCard({ listing }: { listing: Listing }) {
  const editorial = fromListing(listing);
  return (
    <Link
      href={`/marketplace/listing/${listing.slug}`}
      className="editorial-card editorial-card-hover group block p-5 no-underline"
    >
      <div className="flex items-start gap-4">
        <Monogram agent={editorial} size={52} />
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <h3 className="font-editorial-serif text-[22px] leading-tight text-[var(--ink)]">
              {editorial.name}
            </h3>
            <AgentPill tone={editorial.isFree ? "forest" : "neutral"}>{editorial.price}</AgentPill>
          </div>
          <p className="mt-1 font-mono text-[10px] uppercase tracking-[0.14em] text-[var(--mute)]">
            {editorial.role}
          </p>
        </div>
      </div>
      <p className="mt-4 line-clamp-2 font-editorial-serif text-[13.5px] italic leading-6 text-[var(--ink-soft)]">
        &ldquo;{editorial.vibe}&rdquo;
      </p>
      {editorial.tags.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-1.5">
          {editorial.tags.slice(0, 3).map((tag) => (
            <AgentPill key={tag}>{tag}</AgentPill>
          ))}
        </div>
      )}
      <div className="mt-5 flex items-center justify-between border-t border-[var(--edge)] pt-3 text-[12px] text-[var(--mute)]">
        <span className="font-mono uppercase tracking-[0.12em]">View detail</span>
        <span className="font-bold text-[var(--forest)] transition group-hover:translate-x-0.5">
          →
        </span>
      </div>
    </Link>
  );
}
