import Link from "next/link";

import { browseListings } from "@/lib/marketplace/api";
import { ListingCard } from "@/components/marketplace/storefront/Listing/ListingCard";
import { SectionLabel } from "@/components/agents/AgentIdentity";

export default async function Home() {
  let agents: Awaited<ReturnType<typeof browseListings>>["items"] = [];
  try {
    const result = await browseListings({ format: "openclaw", limit: 8 });
    agents = result.items;
  } catch {
    // Backend may be unavailable during local dev; render the page shell.
  }

  return (
    <main className="mx-auto max-w-6xl px-6">
      <section className="border-b border-[var(--edge)] py-16 md:py-24">
        <div className="mb-4 font-mono text-[11px] uppercase tracking-[0.18em] text-[var(--mute)]">
          The marketplace
        </div>
        <h1 className="font-editorial-serif text-5xl leading-[0.98] text-[var(--ink)] md:text-7xl">
          Hire an agent.
          <br />
          <em className="text-[var(--forest)]">Keep your job.</em>
        </h1>
        <p className="mt-6 max-w-xl text-base leading-7 text-[var(--ink-soft)]">
          Curated AI agents you can deploy into your Isol8 container in one click.
          Browse, install, and start delegating.
        </p>
        <div className="mt-8 flex items-center gap-4">
          <Link
            href="/marketplace/agents"
            className="editorial-btn editorial-btn--primary editorial-btn--sm h-10 px-5"
          >
            Browse all agents
          </Link>
          <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-[var(--mute)]">
            {agents.length} featured · free to install
          </span>
        </div>
      </section>

      <section className="py-12">
        <div className="mb-6 flex items-end justify-between">
          <SectionLabel label="Featured agents" count={agents.length} />
          <Link
            href="/marketplace/agents"
            className="font-mono text-[11px] font-bold uppercase tracking-[0.14em] text-[var(--forest)] no-underline"
          >
            View all →
          </Link>
        </div>
        {agents.length === 0 ? (
          <p className="font-editorial-serif text-xl italic text-[var(--mute)]">
            No agents listed yet.
          </p>
        ) : (
          <div className="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
            {agents.map((listing) => (
              <ListingCard key={listing.listing_id} listing={listing} />
            ))}
          </div>
        )}
      </section>
    </main>
  );
}
