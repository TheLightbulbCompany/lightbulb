/**
 * The homepage headline split test (10 tasks 1–2). A visitor is assigned A or
 * B once, kept in a first-party cookie so the same person always sees the same
 * headline, and the variant rides every landing event (a PostHog super
 * property) and the /request-setup form.
 */
export const HEADLINE_COOKIE = "lb_hv";

export const HEADLINES = {
  A: "Your team and your AI employees, in one place.",
  B: "AI employees your whole team can see.",
} as const;

export type HeadlineVariant = keyof typeof HEADLINES;

export function parseHeadlineVariant(value: string | null | undefined): HeadlineVariant | null {
  return value === "A" || value === "B" ? value : null;
}

export function pickHeadlineVariant(): HeadlineVariant {
  return Math.random() < 0.5 ? "A" : "B";
}

/** The visitor's variant from `document.cookie`, or "" before assignment. */
export function readHeadlineVariantCookie(): string {
  if (typeof document === "undefined") return "";
  const m = document.cookie.match(new RegExp(`(?:^|; )${HEADLINE_COOKIE}=([^;]*)`));
  return parseHeadlineVariant(m?.[1]) ?? "";
}

const NINETY_DAYS_S = 60 * 60 * 24 * 90;

/**
 * An inline script for the hero: it keeps the server-picked variant in the
 * cookie while the HTML is still parsing, so PostHog's init (an effect, later)
 * can register it before the first `$pageview` / `landing_viewed`.
 */
export function headlineCookieScript(variant: HeadlineVariant): string {
  return `if(!/(?:^|; )${HEADLINE_COOKIE}=[AB](?:;|$)/.test(document.cookie))document.cookie="${HEADLINE_COOKIE}=${variant}; Path=/; Max-Age=${NINETY_DAYS_S}; SameSite=Lax"`;
}

/** Super properties for PostHog: the visitor's variant, once assigned. */
export function headlineVariantProps(): { headline_variant?: HeadlineVariant } {
  const v = parseHeadlineVariant(readHeadlineVariantCookie());
  return v ? { headline_variant: v } : {};
}
