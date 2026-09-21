/**
 * Official connector brand-mark colours for the onboarding setup band.
 *
 * A vendor's mark is theirs, so recolouring it onto our tokens would
 * misrepresent it — this file is exempt from the brand-token sweep for that
 * reason (`*Brand.ts`), and nothing else in the band may hold a hex.
 *
 * Two of the three are the same case `providerBrand.ts` already documents for
 * the AWS wordmark: Simple Icons records GitHub as #181717 and Notion as
 * #000000, both of which are invisible on the midnight ground. These are
 * MONOCHROME marks whose own brand guidance is "black on light, white on
 * dark", so following the foreground token is the correct rendering, not a
 * compromise — it is what the vendors ask for.
 *
 * Paths and hexes come from Simple Icons (CC0-1.0), fetched rather than typed.
 */
export const CONNECTOR_BRAND = {
  stripe: "#635BFF",
  github: "var(--foreground)",
  notion: "var(--foreground)",
} as const;
