/**
 * Mermaid's own theme slots, as RESOLVED brand hexes — mermaid derives further
 * shades from these with khroma, which needs a parseable colour, not a
 * `var(--token)` it would only see as an opaque string. Mirrors globals.css
 * (--cream-deep, --midnight, --cream-deeper, --cream, --midnight-muted);
 * the only file in components/chat besides providerBrand.ts allowed a literal.
 */
export const LOBSTER_MERMAID_THEME = {
  primaryColor: "#EFE8D9", // --cream-deep
  primaryBorderColor: "#0F1A2B", // --midnight
  primaryTextColor: "#0F1A2B", // --midnight
  secondaryColor: "#E7DECB", // --cream-deeper
  tertiaryColor: "#F5EFE2", // --cream
  lineColor: "#62676D", // --midnight-muted, resolved
  edgeLabelBackground: "#EFE8D9", // --cream-deep
} as const;
