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
 * subscription. The grant comes from the Stripe webhook. This only decides
 * where the user ends up standing; ProvisionGate's status polling observes the
 * server-side grant.
 */
import { APP_SCHEME, WEB_ORIGIN } from "../config/domain";

export function redirectSystemPath({ path, initial }: { path: string; initial: boolean }): string | null {
  const route = path.startsWith(`${WEB_ORIGIN}/`)
    ? path.slice(WEB_ORIGIN.length + 1)
    : path.startsWith(`${APP_SCHEME}://`)
      ? path.slice(APP_SCHEME.length + 3)
      : path;
  // Warm OAuth returns refresh the current room through AppState; navigating
  // to / would unmount its onboarding card before that refresh can settle.
  if (/^\/?checkout_redirect\/(connected|connect_failed)(?:[/?#]|$)/.test(route)) {
    return initial ? "/" : null;
  }
  if (/^\/?checkout_redirect(\/|$)/.test(route)) return "/";
  // The old chat app's screens are gone: a saved or pushed link to one opens Home.
  if (/^\/?(\(app\)\/)?(agent|room\/[^/?#]+\/details|session|task|automation|control|work|workspaces|library|invite)(?:[/?#]|$)/.test(route)) return "/";
  return path;
}
