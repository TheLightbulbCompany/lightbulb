import { APP_SCHEME, WEB_ORIGIN } from "../config/domain";

/** Escape a literal for embedding in a RegExp source. */
const re = (literal: string) => literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const AGENT_LINK_RE = new RegExp(`^${re(APP_SCHEME)}://agent/([^/?#]+)`);

/**
 * Parse an `<scheme>://agent/<agentId>` deep link (optionally with a query
 * string). Returns null for any other scheme/host (e.g. the
 * oauth-native-callback link).
 */
export function parseAgentDeepLink(url: string): { agentId: string } | null {
  const match = AGENT_LINK_RE.exec(url);
  if (!match) return null;
  // Deep links are externally triggerable, so a malformed percent-encoding
  // (e.g. `<scheme>://agent/%`) must return null rather than throw a URIError.
  let agentId: string;
  try {
    agentId = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  return agentId ? { agentId } : null;
}

const CHECKOUT_RETURN_RE = new RegExp(
  `^(?:${re(WEB_ORIGIN)}|${re(APP_SCHEME)}://)/?checkout_redirect/(success|cancel|portal)`,
);
const CONNECTION_RETURN_RE = new RegExp(
  `^(?:${re(WEB_ORIGIN)}|${re(APP_SCHEME)}://)/?checkout_redirect/(connected|connect_failed)(?:[?#]|$)`,
);

/** A return URL only triggers a server refresh; it does not grant a connection. */
export function isConnectionReturn(url: string): boolean {
  return CONNECTION_RETURN_RE.test(url);
}

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
