/**
 * The one place the revenue figure lives.
 *
 * The amount only — no currency symbol, no surrounding prose — so the band can
 * set the unit typographically and a future reuse (a blog post, an OG image)
 * reads the same number rather than re-deriving it.
 *
 * Owner call 2026-09-17: this is a defensible floor, not a tracked metric. The
 * real combined figure was described as "about $2.5M, growing to $3M", which is
 * an estimate rather than something reconstructible from a report — so the site
 * states a round floor it can stand behind under questioning instead of a
 * precise number that invites "from what data?". Bump it when the floor moves,
 * not when the estimate does.
 */
export const COMBINED_ANNUAL_REVENUE = "2M+";
