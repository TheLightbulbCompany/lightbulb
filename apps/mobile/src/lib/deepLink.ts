import { APP_SCHEME, WEB_ORIGIN } from "../config/domain";

/** Escape a literal for embedding in a RegExp source. */
const re = (literal: string) => literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const CHECKOUT_RETURN_RE = new RegExp(
  `^(?:${re(WEB_ORIGIN)}|${re(APP_SCHEME)}://)/?checkout_redirect/(success|cancel|portal)`,
);

/**
 * Recognise the return leg of a Stripe app-to-web Checkout.
 *
 * Two shapes reach us, both of which have to work:
 *   - `<web-origin>/checkout_redirect/success` — the universal link, which
 *     is what Stripe's `success_url` is set to and what iOS hands back when the
 *     domain association has resolved.
 *   - `<scheme>://checkout_redirect/success` — the custom-scheme fallback the
 *     web page offers, for when iOS declines to open the app from the https
 *     link.
 *
 * Returns the status, or null for anything else. Deep links are externally
 * triggerable, so this only ever REPORTS what came back — the caller refetches
 * entitlement from the server. Nothing about a subscription may be granted
 * from a URL a third party can construct.
 */
export function parseCheckoutReturn(url: string): { status: "success" | "cancel" | "portal" } | null {
  const match = CHECKOUT_RETURN_RE.exec(url);
  if (!match) return null;
  return { status: match[1] as "success" | "cancel" | "portal" };
}
