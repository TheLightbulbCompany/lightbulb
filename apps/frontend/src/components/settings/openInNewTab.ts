/**
 * `window.open` returns `null` (not a throw) when the popup is blocked. Both
 * CompanyStripeSection and ApprovalRow need to tell "opened" from "blocked"
 * before treating the Stripe hand-off as done — a blocked popup must not be
 * mistaken for a completed redirect (e.g. auto-approving a Teams approval
 * the owner never actually saw).
 */
export function openInNewTab(url: string): boolean {
  const w = window.open(url, "_blank", "noopener");
  return w !== null;
}
