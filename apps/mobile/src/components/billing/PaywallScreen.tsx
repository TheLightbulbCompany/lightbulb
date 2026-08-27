/**
 * In-app subscription purchase — App Store guidelines 3.1.1 / 3.1.3(b).
 *
 * App Review rejected 0.1.0 (17) holding that a plan sold on flickmeon.ai must also
 * be purchasable inside the app. This is that purchase path.
 *
 * BOTH options are offered WITH THEIR PRICES, which is legal specifically on
 * the U.S. storefront: 3.1.1(a) lets a U.S. app carry buttons and external
 * links to other purchase mechanisms with no entitlement, App Review restated
 * that allowance in the rejection itself, and Apple approved Spotify's U.S.
 * build showing external pricing alongside links in May 2025 — the same shape
 * as this screen.
 *
 * Two conditions come with that allowance, and both are enforced here rather
 * than assumed:
 *
 * - **U.S. storefront only.** Everywhere else, 3.1.3's no-steering rule still
 *   bites and this screen would be a rejectable violation. The gate is
 *   `getStorefront()` — the VIEWER's actual storefront — not the app's
 *   availability settings, because availability is changed in App Store
 *   Connect by someone who will never think to open this file. Until it
 *   resolves, the web option stays hidden: fail closed.
 * - **The displayed price must be accurate.** A stale or wrong external price
 *   is a 5.1.1 violation, not a cosmetic bug, so the web price is fetched from
 *   /billing/account (server-owned) and NEVER baked into the binary, where it
 *   would go wrong the moment pricing changed and could only be fixed by
 *   shipping a build. If the fetch fails the price is simply omitted — an
 *   unpriced link is allowed; a wrong price is not.
 *
 * Apple's own price is `product.displayPrice`, StoreKit's localized string for
 * the viewer's storefront, likewise never hardcoded.
 *
 * IAP is the primary action and stays at least as prominent as the web option:
 * the December 2025 appeals ruling restored Apple's authority over relative
 * prominence, so the web button must not be made louder than Subscribe.
 *
 * The purchase is not trusted here. A StoreKit success hands back a signed
 * transaction, which goes to the backend for verification against Apple's root
 * CA; only that call grants a plan. `finishTransaction` runs after the server
 * confirms, so a crash mid-flow leaves the transaction unfinished and StoreKit
 * redelivers it on next launch rather than losing a paid purchase.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import {
  ErrorCode,
  getAvailablePurchases,
  getStorefront,
  useIAP,
} from "expo-iap";

import { WEB_BILLING_URL } from "../../config/env";
import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { parseCheckoutReturn } from "../../lib/deepLink";
import {
  APPLE_IAP_AVAILABLE,
  APPLE_SUBSCRIPTION_SKU,
  createWebCheckout,
  linkApplePurchase,
  ownerAccountToken,
} from "../../lib/appleIap";

/**
 * Storefronts where an external purchase link + its price may be shown.
 * `getStorefront()` returns an ISO-3166 alpha-3 code; the U.S. carve-out in
 * 3.1.1(a) / 3.1.3 applies to exactly one of them.
 */
const EXTERNAL_LINK_STOREFRONTS = new Set(["USA"]);

export function PaywallScreen({
  api,
  ownerId,
  onPurchased,
}: {
  /** Authenticated client — injected so this stays testable without Clerk. */
  api: {
    get: (path: string) => Promise<unknown>;
    post: (path: string, body: unknown) => Promise<unknown>;
  };
  /**
   * Owner the purchase is bound to, stamped into StoreKit's `appAccountToken`.
   * A prop rather than a `useAuth()` call for the same reason `api` is one.
   */
  ownerId?: string | null;
  onPurchased: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // undefined = not resolved yet. The web option stays hidden until the
  // storefront is known — showing it outside the U.S. is a guideline
  // violation, so an unresolved read must fail closed.
  const [storefront, setStorefront] = useState<string | undefined>(undefined);
  // null = unknown price. An unpriced link is permitted; a WRONG price is a
  // 5.1.1 violation, so a failed fetch omits the figure rather than guessing.
  const [webPriceUsd, setWebPriceUsd] = useState<number | null>(null);
  const [webBusy, setWebBusy] = useState(false);

  // useIAP takes the success handler as an option and RETURNS finishTransaction,
  // so the handler cannot close over it directly (it is declared below). The ref
  // is filled in right after the hook call and only ever read inside the
  // callback, which by definition runs after a purchase.
  const finishRef = useRef<((args: { purchase: never }) => Promise<void>) | null>(null);

  const onPurchaseSuccess = useCallback(
    (purchase: { purchaseToken?: string | null }) => {
      void (async () => {
        // No token means nothing verifiable to send; treat as a failed purchase
        // rather than silently granting anything.
        if (!purchase.purchaseToken) {
          setError("That purchase couldn't be confirmed. Contact support@isol8.co.");
          setBusy(false);
          return;
        }
        try {
          await linkApplePurchase(api.post, purchase.purchaseToken);
          // Only now is the transaction finished: until the server has recorded
          // it, StoreKit redelivering is exactly what we want.
          await finishRef.current?.({ purchase: purchase as never });
          onPurchased();
        } catch {
          setError(
            "Your purchase went through, but we couldn't activate it yet. Reopen the app, or contact support@isol8.co.",
          );
        } finally {
          setBusy(false);
        }
      })();
    },
    [api, onPurchased],
  );

  const {
    connected,
    subscriptions,
    fetchProducts,
    requestPurchase,
    finishTransaction,
    restorePurchases,
  } = useIAP({
      onPurchaseSuccess,
      onPurchaseError: (e) => {
        setBusy(false);
        // Backing out of the Apple sheet is not an error worth shouting about.
        if (e?.code !== ErrorCode.UserCancelled) {
          setError("The purchase didn't complete. Nothing was charged.");
        }
      },
    });

  finishRef.current = finishTransaction as typeof finishRef.current;

  useEffect(() => {
    if (!connected || !APPLE_IAP_AVAILABLE) return;
    void fetchProducts({ skus: [APPLE_SUBSCRIPTION_SKU], type: "subs" });
  }, [connected, fetchProducts]);

  // Storefront decides whether the external link may appear at all.
  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    void getStorefront()
      .then((code: string) => {
        if (!cancelled) setStorefront(code);
      })
      // Leave it unresolved on failure: hidden beats a violation.
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [connected]);

  // Web price, server-owned so it cannot go stale in the binary.
  useEffect(() => {
    let cancelled = false;
    void api
      .get("/billing/account")
      .then((body) => {
        const price = (body as { flat_price_usd?: number })?.flat_price_usd;
        if (!cancelled && typeof price === "number") setWebPriceUsd(price);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api]);

  // Return leg of the web checkout. Stripe's success_url is a universal link
  // back into the app, and the fallback page offers the flickmeon:// scheme when
  // iOS declines to open it — both land here.
  //
  // A returning URL is only a HINT to go re-read entitlement, never a grant.
  // Deep links are externally triggerable, so treating one as proof of payment
  // would hand anyone a free subscription; the subscription is granted by the
  // customer.subscription.* webhook and confirmed by refetching the server.
  useEffect(() => {
    const sub = Linking.addEventListener("url", ({ url }) => {
      if (parseCheckoutReturn(url)?.status === "success") onPurchased();
    });
    return () => sub.remove();
  }, [onPurchased]);

  const showWebOption =
    storefront !== undefined && EXTERNAL_LINK_STOREFRONTS.has(storefront);

  const product = subscriptions.find((s) => s.id === APPLE_SUBSCRIPTION_SKU);

  async function onSubscribe() {
    if (busy || !product) return;
    setBusy(true);
    setError(null);
    try {
      // Binds the transaction to this account INSIDE the payload Apple signs.
      // Without it the server's cross-account check has nothing to compare and
      // silently passes, leaving only a lookup on an eventually-consistent GSI
      // — which two accounts submitting the same transaction can both clear.
      const appAccountToken = ownerId ? await ownerAccountToken(ownerId) : undefined;
      await requestPurchase({
        type: "subs",
        request: { apple: { sku: APPLE_SUBSCRIPTION_SKU, appAccountToken } },
      });
    } catch {
      setBusy(false);
      setError("Couldn't start the purchase. Try again in a moment.");
    }
  }

  /**
   * Hand off to the web checkout. Asks the server for a Checkout Session and
   * opens it in the DEFAULT BROWSER — `Linking.openURL`, never a WebView (see
   * lib/appleIap). Falls back to the billing page if the session cannot be
   * created, so the option is never a dead button.
   */
  async function onWebCheckout() {
    if (webBusy) return;
    setWebBusy(true);
    setError(null);
    try {
      const url = await createWebCheckout(api.post);
      await Linking.openURL(url ?? WEB_BILLING_URL);
    } catch {
      await Linking.openURL(WEB_BILLING_URL).catch(() => {
        setError("Couldn't open checkout. Try again in a moment.");
      });
    } finally {
      setWebBusy(false);
    }
  }

  /**
   * Restore a prior purchase — the path a reinstall or a new device depends on.
   *
   * `restorePurchases()` only REFRESHES the hook's available-purchases state;
   * it does not fire `onPurchaseSuccess` for each restored transaction. Calling
   * it and stopping there — which is what this did — meant Restore appeared to
   * work, sent nothing to the server, and left a paying customer with no way to
   * recover entitlement after reinstalling. Each restored token has to go
   * through the same verify-then-finish path a fresh purchase takes.
   *
   * The transactions come from the MODULE-level `getAvailablePurchases()`,
   * which resolves to the array, and never from the hook's `availablePurchases`
   * state. The hook's own `getAvailablePurchases` returns `Promise<void>` — it
   * only schedules a state update, which cannot reach a callback that already
   * captured `availablePurchases` at the render where Restore was tapped. That
   * closure is empty on a fresh launch, so reading it told a paying customer
   * "No previous purchase found" while their subscription sat right there.
   */
  async function onRestore() {
    setBusy(true);
    setError(null);
    try {
      // Prompts for the Apple Account and syncs the transaction history — the
      // part a reinstall on a new device actually needs.
      await restorePurchases();
      const purchases = (await getAvailablePurchases()) ?? [];

      const restored = purchases.filter(
        (p) => p?.id === APPLE_SUBSCRIPTION_SKU || p?.productId === APPLE_SUBSCRIPTION_SKU,
      );
      const linked = await Promise.allSettled(
        restored
          .filter((p) => p?.purchaseToken)
          .map(async (p) => {
            await linkApplePurchase(api.post, p.purchaseToken as string);
            await finishRef.current?.({ purchase: p as never });
          }),
      );

      const ok = linked.filter((r) => r.status === "fulfilled").length;
      if (ok > 0) {
        onPurchased();
      } else if (restored.length > 0) {
        // Purchases exist but none could be recorded — an honest failure beats
        // silence, since the customer is trying to recover something they paid
        // for and would otherwise assume it simply did not exist.
        setError("We found a purchase but couldn't restore it. Contact support@isol8.co.");
      } else {
        setError("No previous purchase found on this Apple Account.");
      }
    } catch {
      setError("Couldn't restore purchases. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <ScrollView style={styles.page} contentContainerStyle={styles.content} testID="paywall">
      <Text style={styles.title}>Lightbulb AI</Text>
      <Text style={styles.lede}>
        Keep your agents running around the clock, with a higher monthly compute
        allowance.
      </Text>

      {APPLE_IAP_AVAILABLE && (
        <View style={styles.card}>
          <Text style={styles.overline}>SUBSCRIPTION</Text>
          {product ? (
            <>
              {/* StoreKit's own localized price — never a hardcoded number. */}
              <Text style={styles.price}>{product.displayPrice}</Text>
              <Text style={styles.caption}>
                Billed monthly through your Apple Account. Cancel any time in
                iOS Settings.
              </Text>
            </>
          ) : (
            <Text style={styles.caption}>
              {connected ? "Loading plan…" : "Connecting to the App Store…"}
            </Text>
          )}

          <Pressable
            onPress={() => void onSubscribe()}
            disabled={busy || !product}
            accessibilityRole="button"
            style={({ pressed }) => [
              styles.primary,
              (pressed || busy || !product) && styles.dim,
            ]}
          >
            {busy ? (
              <ActivityIndicator color={colors.white} />
            ) : (
              <Text style={styles.primaryText}>Subscribe</Text>
            )}
          </Pressable>

          <Pressable
            onPress={() => void onRestore()}
            disabled={busy}
            accessibilityRole="button"
            style={({ pressed }) => [styles.linkRow, pressed && styles.dim]}
          >
            <Text style={styles.linkText}>Restore purchases</Text>
          </Pressable>
        </View>
      )}

      {showWebOption && (
        <View style={styles.card}>
          <Text style={styles.overline}>OTHER WAYS TO SUBSCRIBE</Text>
          {/* Price shown only when the server supplied it — an unpriced link
              is allowed, a wrong price is a 5.1.1 violation. */}
          <Text style={styles.caption}>
            {webPriceUsd != null
              ? `You can also subscribe on flickmeon.ai in your browser for $${webPriceUsd}/month, and manage billing there.`
              : "You can also subscribe on flickmeon.ai in your browser, and manage billing there."}
          </Text>
          <Pressable
            onPress={() => void onWebCheckout()}
            disabled={webBusy}
            accessibilityRole="link"
            style={({ pressed }) => [styles.linkRow, (pressed || webBusy) && styles.dim]}
          >
            <Text style={styles.linkText}>
              {webBusy ? "Opening checkout…" : "Subscribe on flickmeon.ai"}
            </Text>
          </Pressable>
        </View>
      )}

      {error != null && <Text style={styles.error}>{error}</Text>}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: { padding: spacing.xl, gap: spacing.lg },
  title: { ...type.display, fontFamily: fonts.loraBold, textAlign: "center" },
  lede: { ...type.body, color: colors.inkSoft, textAlign: "center" },
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  overline: { ...type.overline },
  price: {
    fontFamily: fonts.dmSansBold,
    fontSize: 24,
    lineHeight: 30,
    color: colors.ink,
  },
  caption: { ...type.caption },
  primary: {
    marginTop: spacing.sm,
    backgroundColor: colors.forest,
    borderRadius: radii.lg,
    paddingVertical: spacing.md,
    alignItems: "center",
  },
  primaryText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 22,
    color: colors.white,
  },
  linkRow: { alignSelf: "flex-start", borderRadius: radii.xs },
  linkText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
  error: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
  dim: { opacity: 0.6 },
});
