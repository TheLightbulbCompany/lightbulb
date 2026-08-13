/**
 * In-app subscription purchase — App Store guidelines 3.1.1 / 3.1.3(b).
 *
 * App Review rejected 0.1.0 (17) holding that a plan sold on isol8.co must also
 * be purchasable inside the app. This is that purchase path.
 *
 * BOTH options are offered, which is legal specifically on the U.S. storefront:
 * 3.1.1(a) lets a U.S. app carry buttons and external links to other purchase
 * mechanisms with no entitlement, and App Review restated that allowance in the
 * rejection itself. So StoreKit is the primary action and "subscribe on the
 * web" sits beneath it — the customer chooses, and we keep the cheaper channel
 * available. If this ever ships to a non-U.S. storefront the web option has to
 * come out; that is the only reason it is a named constant rather than inline.
 *
 * The price is NEVER hardcoded. `product.displayPrice` is StoreKit's localized
 * string for the viewer's storefront and is the only correct thing to render.
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
import { ErrorCode, useIAP } from "expo-iap";

import { WEB_BILLING_URL } from "../../config/env";
import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { APPLE_IAP_AVAILABLE, APPLE_SUBSCRIPTION_SKU, linkApplePurchase } from "../../lib/appleIap";

/** Permitted only on the U.S. storefront (3.1.1(a)). See the module docstring. */
const SHOW_WEB_PURCHASE_OPTION = true;

export function PaywallScreen({
  post,
  onPurchased,
}: {
  /** Authenticated POST — `useApi`-shaped, injected so this stays testable. */
  post: (path: string, body: unknown) => Promise<unknown>;
  onPurchased: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
          await linkApplePurchase(post, purchase.purchaseToken);
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
    [post, onPurchased],
  );

  const { connected, subscriptions, fetchProducts, requestPurchase, finishTransaction, restorePurchases } =
    useIAP({
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

  const product = subscriptions.find((s) => s.id === APPLE_SUBSCRIPTION_SKU);

  async function onSubscribe() {
    if (busy || !product) return;
    setBusy(true);
    setError(null);
    try {
      await requestPurchase({
        type: "subs",
        request: { apple: { sku: APPLE_SUBSCRIPTION_SKU } },
      });
    } catch {
      setBusy(false);
      setError("Couldn't start the purchase. Try again in a moment.");
    }
  }

  async function onRestore() {
    setBusy(true);
    setError(null);
    try {
      await restorePurchases();
    } finally {
      setBusy(false);
    }
  }

  return (
    <ScrollView style={styles.page} contentContainerStyle={styles.content} testID="paywall">
      <Text style={styles.title}>Isol8</Text>
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

      {SHOW_WEB_PURCHASE_OPTION && (
        <View style={styles.card}>
          <Text style={styles.overline}>OTHER WAYS TO SUBSCRIBE</Text>
          <Text style={styles.caption}>
            You can also subscribe on isol8.co in your browser, and manage
            billing there.
          </Text>
          <Pressable
            onPress={() => void Linking.openURL(WEB_BILLING_URL)}
            accessibilityRole="link"
            style={({ pressed }) => [styles.linkRow, pressed && styles.dim]}
          >
            <Text style={styles.linkText}>Subscribe on isol8.co</Text>
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
