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
