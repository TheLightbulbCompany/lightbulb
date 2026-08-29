/**
 * `window.open` returns `null` (not a throw) when the popup is blocked. Both
 * CompanyStripeSection and ApprovalRow need to tell "opened" from "blocked"
 * before treating the Stripe hand-off as done — a blocked popup must not be
 * mistaken for a completed redirect (e.g. auto-approving a Teams approval
 * the owner never actually saw).
 *
 * The `noopener` window FEATURE cannot be used here: per spec it makes
 * `window.open` return `null` even on success, so "blocked" and "opened"
 * become indistinguishable and every open reports blocked. Clearing
 * `opener` on the returned window gives the same severing with a usable
 * return value.
 */
export function openInNewTab(url: string): boolean {
  const w = window.open(url, "_blank");
  if (w) {
    w.opener = null;
  }
  return w !== null;
}
