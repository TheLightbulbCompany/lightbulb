/**
 * Where a subscriber goes to manage or cancel, which depends entirely on who
 * took the money.
 *
 * The two channels are mutually exclusive and neither can see the other:
 *
 * - **Apple** — managed ONLY in iOS Settings. Stripe has no record of the
 *   subscription, so its customer portal would 404 or show an unrelated empty
 *   account. Apple's guidelines also expect an app selling auto-renewable
 *   subscriptions to point here.
 * - **Stripe** — managed ONLY in the Stripe customer portal. iOS Settings has
 *   no record of it, so a customer sent there sees "no subscriptions" and
 *   concludes they have already cancelled when they have not.
 *
 * Getting this backwards strands someone trying to cancel, which is the
 * complaint that produces chargebacks and 1-star reviews rather than a support
 * ticket. The app only offers Apple's management surface (App Store 3.1.3(f):
 * no billing link out of the app), so this predicate decides whether to show it.
 *
 * Unknown source resolves to Stripe deliberately: every row predating Apple
 * IAP has no `subscription_source` at all, and those are all Stripe.
 */
export type SubscriptionSource = string | null | undefined;

export function isAppleManaged(source: SubscriptionSource): boolean {
  return source === "apple_iap";
}
