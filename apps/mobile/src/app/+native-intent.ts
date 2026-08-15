/**
 * Rewrites incoming deep links that have no screen of their own.
 *
 * `/checkout_redirect/{success,cancel,portal}` are Stripe return URLs, not
 * destinations. They exist so iOS hands control back to the app instead of
 * stranding the customer in Safari — the associated-domains entry in
 * `apps/frontend/public/.well-known/apple-app-site-association` claims
 * `/checkout_redirect/*` precisely so those URLs open the app.
 *
 * But claiming a path only gets the app opened; Expo Router still tries to
 * navigate to it, and there is deliberately no `checkout_redirect` route. Left
 * alone, a customer who has just paid lands on the unmatched-route screen —
 * the worst possible moment to show someone a dead end, and one App Review
 * walks straight into if they use the web purchase option.
 *
 * Entitlement is NOT granted here and must not be: a deep link is externally
 * triggerable, so treating one as proof of payment would hand anyone a free
 * subscription. The grant comes from the Stripe webhook. PaywallScreen's own
 * `Linking` listener separately sees the same URL and refetches the server's
 * view — this only decides where the user ends up standing, which is the app's
 * normal entry point in every case, success or cancel alike.
 */
export function redirectSystemPath({ path }: { path: string; initial: boolean }): string {
  if (/^\/?checkout_redirect(\/|$)/.test(path)) return "/";
  return path;
}
