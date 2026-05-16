import Link from "next/link";

import { featuredListings } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

export async function MarketplaceSection() {
  let items: Listing[] = [];
  try {
    const result = await featuredListings({ limit: 6 });
    items = result.items;
  } catch {
    // Backend may be unavailable during local dev / build — render the
    // section shell with the CTA only. Never break the landing render.
  }

  return (
    <section className="landing-marketplace" id="marketplace">
      <div className="section-inner">
        <div className="marketplace-intro">
          <p className="section-eyebrow reveal">The Marketplace</p>
          <h2 className="section-h2 reveal">
            Hire an agent.
          </h2>
          <p className="marketplace-lede">
            A growing storefront of production-grade agents you can deploy into
            your isol8 container in one click. <strong>Curated, vetted, and
            yours to own.</strong>
          </p>
          <Link href="/marketplace" className="marketplace-cta-inline">
            Browse the marketplace →
          </Link>
        </div>

        {items.length > 0 && (
          <div className="marketplace-grid">
            {items.map((listing) => (
              <Link
                key={listing.listing_id}
                href={`/marketplace/listing/${listing.slug}`}
                className="marketplace-card"
              >
                <div className="marketplace-card-head">
                  <span className="marketplace-card-name">{listing.name}</span>
                  <span className="marketplace-card-tier">
                    {listing.price_cents === 0 ? "Free" : "Premium"}
                  </span>
                </div>
                <p className="marketplace-card-desc">
                  {firstLine(listing.description_md)}
                </p>
                {listing.tags.length > 0 && (
                  <div className="marketplace-card-tags">
                    {listing.tags.slice(0, 3).map((t) => (
                      <span key={t} className="marketplace-card-tag">
                        {t}
                      </span>
                    ))}
                  </div>
                )}
                <div className="marketplace-card-foot">
                  <span className="marketplace-card-action">View →</span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function firstLine(md: string, max = 140): string {
  const trimmed = md.trim().split(/\n{2,}/, 1)[0]?.replace(/[#*_`>]/g, "").trim() ?? "";
  if (trimmed.length <= max) return trimmed;
  return trimmed.slice(0, max - 1).trimEnd() + "…";
}
