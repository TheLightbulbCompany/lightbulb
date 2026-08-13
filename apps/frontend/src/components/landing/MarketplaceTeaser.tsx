import Link from "next/link";

import { browseListings } from "@/lib/marketplace/api";
import type { Listing } from "@/lib/marketplace/types";

/**
 * Marketplace teaser — live listings from the public search endpoint
 * (GET /marketplace/listings/search, 60s ISR via browseListings), falling
 * back to six illustrative static cards when the API errors or is empty.
 * Server component: no client fetch, no loading state.
 */

type TeaserCard = {
  key: string;
  name: string;
  desc: string;
  tags: string[];
  price?: string;
  hires?: number;
};

const FALLBACK: TeaserCard[] = [
  {
    key: "Scout",
    name: "Scout",
    desc: "Market researcher. Sizes a niche, maps competitors, and finds the gap worth entering.",
    tags: ["research", "strategy"],
  },
  {
    key: "Quill",
    name: "Quill",
    desc: "Content writer. Keeps the blog, the newsletter, and the socials moving in your voice.",
    tags: ["content", "social"],
  },
  {
    key: "Halo",
    name: "Halo",
    desc: "Customer support. Answers the inbox, resolves the easy ones, escalates the rest.",
    tags: ["support", "email"],
  },
  {
    key: "Ledger",
    name: "Ledger",
    desc: "Bookkeeper. Categorizes every transaction and hands you a clean month-end close.",
    tags: ["finance", "ops"],
  },
  {
    key: "Beacon",
    name: "Beacon",
    desc: "SEO specialist. Finds the queries you should own and grinds the rankings up.",
    tags: ["seo", "growth"],
  },
  {
    key: "Pace",
    name: "Pace",
    desc: "Operations. Chases schedules, suppliers, and follow-ups so nothing slips.",
    tags: ["ops", "scheduling"],
  },
];

/** First readable sentence-ish run of the listing's markdown description. */
function snippet(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^[#>\s*-]+/gm, "")
    .replace(/[*_`~]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function toCard(l: Listing): TeaserCard {
  return {
    key: l.listing_id,
    name: l.name,
    desc: snippet(l.description_md ?? ""),
    tags: (l.tags ?? []).slice(0, 3),
    price: l.price_cents > 0 ? `$${(l.price_cents / 100).toFixed(0)}` : "Free",
    hires: l.install_count,
  };
}

async function fetchCards(): Promise<TeaserCard[]> {
  try {
    const { items } = await browseListings({ limit: 6, format: "openclaw" });
    const cards = items.map(toCard);
    return cards.length > 0 ? cards : FALLBACK;
  } catch {
    return FALLBACK;
  }
}

export async function MarketplaceTeaser() {
  const cards = await fetchCards();

  return (
    <section className="ld-mkt" id="marketplace">
      <div className="ld-section">
        <div className="ld-mkt-head">
          <div>
            <div className="ld-kicker">the marketplace</div>
            <h2 className="ld-h2">
              Grow the team <em>as you grow.</em>
            </h2>
            <p className="ld-lede">
              When the work outgrows Friday and Atlas, hire specialists in one
              click. Each one joins your roster, takes its lane, and reports
              in like the rest of the team.
            </p>
          </div>
          <Link href="/marketplace" className="ld-mkt-link">
            Browse the marketplace <span aria-hidden="true">→</span>
          </Link>
        </div>

        <div className="ld-shelf ld-shelf--teaser">
          {cards.map((card) => (
            <div key={card.key} className="ld-listing">
              <div className="ld-listing-head">
                <span className="ld-puck">
                  {card.name.slice(0, 2).toUpperCase()}
                </span>
                {card.price && <span className="ld-price">{card.price}</span>}
              </div>
              <h3>{card.name}</h3>
              <p className="ld-listing-desc">{card.desc}</p>
              <div className="ld-listing-tags">
                {card.tags.map((t) => (
                  <span key={t} className="ld-listing-tag">
                    {t}
                  </span>
                ))}
              </div>
              {typeof card.hires === "number" && card.hires > 0 && (
                <div className="ld-listing-foot">
                  {card.hires} {card.hires === 1 ? "hire" : "hires"}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
