/**
 * Parse an `isol8://agent/<agentId>` deep link (optionally with a query string).
 * Returns null for any other scheme/host (e.g. the oauth-native-callback link).
 */
export function parseAgentDeepLink(url: string): { agentId: string } | null {
  const match = /^isol8:\/\/agent\/([^/?#]+)/.exec(url);
  if (!match) return null;
  // Deep links are externally triggerable, so a malformed percent-encoding
  // (e.g. `isol8://agent/%`) must return null rather than throw a URIError.
  let agentId: string;
  try {
    agentId = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  return agentId ? { agentId } : null;
}

/**
 * Recognise the return leg of a Stripe app-to-web Checkout.
 *
 * Two shapes reach us, both of which have to work:
 *   - `https://isol8.co/checkout_redirect/success` — the universal link, which
 *     is what Stripe's `success_url` is set to and what iOS hands back when the
 *     domain association has resolved.
 *   - `isol8://checkout_redirect/success` — the custom-scheme fallback the web
 *     page offers, for when iOS declines to open the app from the https link.
 *
 * Returns the status, or null for anything else. Deep links are externally
 * triggerable, so this only ever REPORTS what came back — the caller refetches
 * entitlement from the server. Nothing about a subscription may be granted
 * from a URL a third party can construct.
 */
export function parseCheckoutReturn(url: string): { status: "success" | "cancel" } | null {
  const match = /^(?:https:\/\/isol8\.co|isol8:\/\/)\/?checkout_redirect\/(success|cancel)/.exec(url);
  if (!match) return null;
  return { status: match[1] as "success" | "cancel" };
}
