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
 * ticket. Hence a single function, driven off the server's
 * `subscription_source`, instead of a guess at each call site.
 *
 * Unknown source resolves to Stripe deliberately: every row predating Apple
 * IAP has no `subscription_source` at all, and those are all Stripe.
 */
import { Linking } from "react-native";

import { WEB_BILLING_URL } from "../config/env";

export type SubscriptionSource = string | null | undefined;

export function isAppleManaged(source: SubscriptionSource): boolean {
  return source === "apple_iap";
}

/**
 * Open the correct management surface.
 *
 * `openAppleSubscriptions` is injected (expo-iap's `deepLinkToSubscriptions`)
 * so this stays unit-testable without the native module. `portalUrl` is
 * fetched by the caller from POST /billing/portal — passing it in keeps the
 * network call at the call site, where the auth client already lives.
 */
export async function openSubscriptionManagement({
  source,
  portalUrl,
  openAppleSubscriptions,
}: {
  source: SubscriptionSource;
  portalUrl: string | null;
  openAppleSubscriptions: () => Promise<void>;
}): Promise<void> {
  if (isAppleManaged(source)) {
    await openAppleSubscriptions();
    return;
  }
  // Stripe. Falls back to the web billing page when the portal session could
  // not be created, so the row is never a dead button — an unreachable portal
  // must still leave the customer somewhere they can act.
  await Linking.openURL(portalUrl ?? WEB_BILLING_URL);
}
