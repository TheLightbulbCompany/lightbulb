import Link from "next/link";

/**
 * Static marketplace teaser — six illustrative agent cards (no fetch; the
 * real shelf lives at /marketplace, which is already public). Reuses the
 * ld-mkt forest-section styling the old fetched shelf used.
 */

const AGENTS = [
  {
    name: "Scout",
    desc: "Market researcher. Sizes a niche, maps competitors, and finds the gap worth entering.",
    tags: ["research", "strategy"],
  },
  {
    name: "Quill",
    desc: "Content writer. Keeps the blog, the newsletter, and the socials moving in your voice.",
    tags: ["content", "social"],
  },
  {
    name: "Halo",
    desc: "Customer support. Answers the inbox, resolves the easy ones, escalates the rest.",
    tags: ["support", "email"],
  },
  {
    name: "Ledger",
    desc: "Bookkeeper. Categorizes every transaction and hands you a clean month-end close.",
    tags: ["finance", "ops"],
  },
  {
    name: "Beacon",
    desc: "SEO specialist. Finds the queries you should own and grinds the rankings up.",
    tags: ["seo", "growth"],
  },
  {
    name: "Pace",
    desc: "Operations. Chases schedules, suppliers, and follow-ups so nothing slips.",
    tags: ["ops", "scheduling"],
  },
];

export function MarketplaceTeaser() {
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
          {AGENTS.map((agent) => (
            <div key={agent.name} className="ld-listing">
              <div className="ld-listing-head">
                <span className="ld-puck">{agent.name.slice(0, 2).toUpperCase()}</span>
              </div>
              <h3>{agent.name}</h3>
              <p className="ld-listing-desc">{agent.desc}</p>
              <div className="ld-listing-tags">
                {agent.tags.map((t) => (
                  <span key={t} className="ld-listing-tag">
                    {t}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
