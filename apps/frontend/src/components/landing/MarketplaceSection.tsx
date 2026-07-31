import Link from "next/link";

import { featuredListings } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

export async function MarketplaceSection() {
  let items: Listing[] = [];
  try {
    const result = await featuredListings({ limit: 3 });
    items = result.items;
  } catch {
    // Backend may be unavailable during local dev / build — render the
    // section shell with the CTA only. Never break the landing render.
  }

  return (
    <section className="ld-mkt" id="marketplace">
      <div className="ld-section">
        <div className="ld-mkt-head">
          <div>
            <div className="ld-kicker" data-ld-reveal>
              the marketplace
            </div>
            <h2 className="ld-h2" data-ld-reveal>
              Staff up in <em>one click.</em>
            </h2>
            <p className="ld-lede" data-ld-reveal>
              A growing storefront of production-grade agents you can drop
              straight into your business. Need a sales rep, a bookkeeper, a
              researcher? Hire one and it starts working today.{" "}
              <strong>Curated, vetted, and yours to own.</strong>
            </p>
          </div>
          <Link href="/marketplace" className="ld-mkt-link" data-ld-reveal>
            Browse all <span aria-hidden="true">→</span>
          </Link>
        </div>

        {items.length > 0 && (
          <div className="ld-shelf" data-ld-reveal-group>
            {items.map((listing) => (
              <Link
                key={listing.listing_id}
                href={`/marketplace/listing/${listing.listing_id}`}
                className="ld-listing"
              >
                <div className="ld-listing-head">
                  <span className="ld-puck">{monogram(listing.name)}</span>
                  <span className="ld-price">
                    {formatPrice(listing.price_cents)}
                  </span>
                </div>
                <h3>{listing.name}</h3>
                <p className="ld-listing-desc">{firstLine(listing.description_md)}</p>
                {listing.tags.length > 0 && (
                  <div className="ld-listing-tags">
                    {listing.tags.slice(0, 3).map((t) => (
                      <span key={t} className="ld-listing-tag">
                        {t}
                      </span>
                    ))}
                  </div>
                )}
                <div className="ld-listing-foot">Deploy →</div>
              </Link>
            ))}

            <Link href="/marketplace" className="ld-listing ld-listing--more">
              <span className="ld-more-glyph">∞</span>
              <span className="ld-more-lbl">The full shelf</span>
              <div className="ld-listing-foot">Browse all →</div>
            </Link>
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

function monogram(name: string): string {
  return name.trim().charAt(0).toUpperCase() || "?";
}

function formatPrice(cents: number): string {
  if (cents === 0) return "$0";
  const dollars = cents / 100;
  return dollars % 1 === 0 ? `$${dollars}` : `$${dollars.toFixed(2)}`;
}
