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
          <div className="shelf">
            <div className="shelf-frame">
              <span className="shelf-frame-label">marketplace · featured</span>
            </div>

            <div className="shelf-grid">
              {items.map((listing) => (
                <Link
                  key={listing.listing_id}
                  href={`/marketplace/listing/${listing.slug}`}
                  className="listing"
                >
                  <div className="listing-head">
                    <span className="puck">{monogram(listing.name)}</span>
                    <span className={priceClass(listing.price_cents)}>
                      {formatPrice(listing.price_cents)}
                    </span>
                  </div>
                  <h3 className="listing-name">{listing.name}</h3>
                  <p className="listing-desc">{firstLine(listing.description_md)}</p>
                  {listing.tags.length > 0 && (
                    <div className="listing-tags">
                      {listing.tags.slice(0, 3).map((t) => (
                        <span key={t} className="listing-tag">
                          {t}
                        </span>
                      ))}
                    </div>
                  )}
                  <div className="listing-foot">
                    <span className="listing-cta">Deploy →</span>
                  </div>
                </Link>
              ))}

              <Link href="/marketplace" className="listing more">
                <span className="more-glyph">∞</span>
                <span className="more-lbl">The full shelf</span>
                <span className="more-cta">Browse all →</span>
              </Link>
            </div>
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

function priceClass(cents: number): string {
  return cents === 0 ? "price free" : "price";
}
