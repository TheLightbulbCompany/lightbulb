/**
 * Apple In-App Purchase config + the one server call the purchase flow makes.
 *
 * The product id is the ONLY commerce constant here. Price, currency, and the
 * localized period string all come from StoreKit at runtime via
 * `product.displayPrice` — Apple requires the displayed price to be the store's
 * own (it varies by storefront and changes without an app release), and
 * hardcoding "$64.99" would be wrong in every other country the moment we ship
 * beyond the U.S.
 *
 * EXPO_PUBLIC_ prefix because Expo inlines only those into the client bundle.
 * Empty default: the Paid Apps Agreement must be active before a subscription
 * product can exist in App Store Connect, so the paywall treats "unset" as
 * "IAP unavailable" rather than crashing.
 */
export const APPLE_SUBSCRIPTION_SKU =
  process.env.EXPO_PUBLIC_APPLE_IAP_PRODUCT_ID ?? "";

export const APPLE_IAP_AVAILABLE = APPLE_SUBSCRIPTION_SKU !== "";

/**
 * Hand a completed StoreKit transaction to the backend for verification.
 *
 * `purchaseToken` is expo-iap's unified name for what is, on iOS, the StoreKit 2
 * signed transaction JWS. The client never decides whether a purchase is valid:
 * it forwards the signed blob, and the server verifies it against Apple's root
 * CA before granting anything (see core/services/apple_iap.py).
 */
export async function linkApplePurchase(
  post: (path: string, body: unknown) => Promise<unknown>,
  purchaseToken: string,
): Promise<void> {
  await post("/billing/apple/link", { signed_transaction: purchaseToken });
}

/**
 * Ask the server for an app-to-web Stripe Checkout URL.
 *
 * Returns a session built with `origin_context: "mobile_app"` — Stripe's
 * app-optimized page, with Apple Pay on by default.
 *
 * The caller MUST open it with `Linking.openURL`, never a WebView. Apple's
 * wording is the binding source, quoted from our own 0.1.0 (17) rejection:
 * U.S.-storefront apps "may link out to the DEFAULT BROWSER, using buttons,
 * external links, or other calls to action, for payment mechanisms other than
 * in-app purchase." An embedded checkout is not linking out — it is the in-app
 * storefront the same guideline forbids. Stripe's iOS guide independently
 * shows this shape: its section is titled "Open Checkout in Safari" and its
 * sample calls UIApplication.shared.open, which is what Linking.openURL binds
 * to on iOS.
 */
export async function createWebCheckout(
  post: (path: string, body: unknown) => Promise<unknown>,
): Promise<string | null> {
  const body = (await post("/billing/apple/web-checkout", {})) as { url?: string };
  return typeof body?.url === "string" ? body.url : null;
}
