/**
 * In-app subscription purchase (App Store guideline 3.1.1).
 *
 * Which products exist is the SERVER's call (`productIds`, from
 * /billing/account); prices are StoreKit's `displayPrice` for the viewer's
 * storefront. Nothing here mentions another way to pay.
 *
 * The purchase is not trusted here. StoreKit's signed transaction goes to the
 * backend, which verifies it against Apple's root CA; only that call grants a
 * plan. `finishTransaction` runs strictly AFTER the server confirms, so a
 * failure or crash mid-flow leaves the transaction unfinished and StoreKit
 * redelivers it on the next launch instead of losing a paid purchase.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { ErrorCode, getAvailablePurchases, useIAP } from "expo-iap";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import {
  type ApiPost,
  linkApplePurchase,
  PLAN_ORDER,
  PurchaseRefused,
  startPurchaseIntent,
} from "../../lib/appleIap";

const FALLBACK_TITLES: Record<string, string> = {
  "company.thelightbulb.mobile.base.monthly": "Base",
  "company.thelightbulb.mobile.plan.monthly": "Workspace",
  "company.thelightbulb.mobile.ultra.monthly": "Ultra",
};

const LINK_PENDING =
  "Your purchase went through, but we couldn't activate it yet. It will finish the next time you open the app.";
const LINK_STRIPE_MANAGED =
  "This workspace is already billed another way, so this App Store purchase wasn't applied. Contact support@thelightbulb.company for a refund.";

type Purchase = { purchaseToken?: string | null; productId?: string; id?: string };

/** The link's 409 when Stripe started billing between intent and link. */
function isStripeManaged(e: unknown): boolean {
  const err = e as { status?: number; body?: { detail?: string } } | null;
  return err?.status === 409 && err.body?.detail === "stripe_managed";
}

export function PaywallScreen({
  api,
  productIds,
  currentProductId,
  onPurchased,
}: {
  /** Authenticated client — injected so this stays testable without Clerk. */
  api: { get: (path: string) => Promise<unknown>; post: ApiPost };
  productIds: string[];
  currentProductId: string | null;
  onPurchased: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const setBusyBoth = (v: boolean) => {
    busyRef.current = v;
    setBusy(v);
  };

  // useIAP takes the success handler as an option and RETURNS finishTransaction,
  // so the handler reads it through a ref filled in right after the hook call.
  const finishRef = useRef<((args: { purchase: never; isConsumable: boolean }) => Promise<unknown>) | null>(
    null,
  );

  /** Link on the server, then — and only then — finish. Throws on link failure. */
  const linkAndFinish = useCallback(
    async (purchase: Purchase & { purchaseToken: string }) => {
      await linkApplePurchase(api.post, purchase.purchaseToken);
      await finishRef.current?.({ purchase: purchase as never, isConsumable: false });
    },
    [api],
  );

  const onPurchaseSuccess = useCallback(
    (purchase: Purchase) => {
      void (async () => {
        try {
          if (!purchase.purchaseToken) {
            setError("That purchase couldn't be confirmed. Contact support@thelightbulb.company.");
            return;
          }
          await linkAndFinish({ ...purchase, purchaseToken: purchase.purchaseToken });
          onPurchased();
        } catch (e) {
          setError(isStripeManaged(e) ? LINK_STRIPE_MANAGED : LINK_PENDING);
        } finally {
          setBusyBoth(false);
        }
      })();
    },
    [linkAndFinish, onPurchased],
  );

  const { connected, subscriptions, fetchProducts, requestPurchase, finishTransaction, restorePurchases } =
    useIAP({
      onPurchaseSuccess,
      onPurchaseError: (e) => {
        setBusyBoth(false);
        // Backing out of the Apple sheet is not an error worth shouting about.
        if (e?.code !== ErrorCode.UserCancelled) {
          setError("The purchase didn't complete. Nothing was charged.");
        }
      },
    });
  finishRef.current = finishTransaction as typeof finishRef.current;

  const skuKey = productIds.join(",");
  useEffect(() => {
    if (!connected || skuKey === "") return;
    void fetchProducts({ skus: skuKey.split(","), type: "subs" });
  }, [connected, fetchProducts, skuKey]);

  const products = subscriptions
    .filter((s) => productIds.includes(s.id))
    .sort((a, b) => (PLAN_ORDER[a.id] ?? 99) - (PLAN_ORDER[b.id] ?? 99));

  if (productIds.length === 0 || products.length === 0) return null;

  async function onBuy(sku: string) {
    if (busyRef.current) return;
    setBusyBoth(true);
    setError(null);
    try {
      const { appAccountToken } = await startPurchaseIntent(api.post);
      await requestPurchase({ type: "subs", request: { apple: { sku, appAccountToken } } });
    } catch (e) {
      setBusyBoth(false);
      setError(
        e instanceof PurchaseRefused && e.reason === "stripe_managed"
          ? "This workspace is billed outside the App Store."
          : "Couldn't start the purchase. Try again in a moment.",
      );
    }
  }

  async function onRestore() {
    if (busyRef.current) return;
    setBusyBoth(true);
    setError(null);
    try {
      await restorePurchases();
      // The MODULE-level call, not the hook's `availablePurchases` state: that
      // state is captured by this closure at the render Restore was tapped on,
      // so on a fresh launch it is empty and stays empty.
      const found = ((await getAvailablePurchases()) as Purchase[]).filter(
        (p): p is Purchase & { purchaseToken: string } =>
          !!p.purchaseToken && productIds.includes(p.productId ?? p.id ?? ""),
      );
      let linked = 0;
      let linkError: unknown = null;
      for (const p of found) {
        try {
          await linkAndFinish(p);
          linked += 1;
        } catch (e) {
          linkError = e; // Left unfinished; StoreKit redelivers it.
        }
      }
      if (linked > 0) onPurchased();
      else if (found.length > 0) setError(isStripeManaged(linkError) ? LINK_STRIPE_MANAGED : LINK_PENDING);
      else setError("No previous purchase found for this Apple Account.");
    } catch {
      setError("Couldn't restore purchases. Try again in a moment.");
    } finally {
      setBusyBoth(false);
    }
  }

  return (
    <ScrollView style={styles.page} contentContainerStyle={styles.content} testID="paywall">
      <Text style={styles.title}>Choose a plan</Text>
      {products.map((p) => (
        <View key={p.id} style={styles.card}>
          <View style={styles.cardText}>
            <Text style={styles.name}>{p.title || FALLBACK_TITLES[p.id] || p.id}</Text>
            <Text style={styles.price}>{`${p.displayPrice}/month`}</Text>
          </View>
          {p.id === currentProductId ? (
            <Text style={styles.caption}>Current plan</Text>
          ) : (
            <Pressable
              onPress={() => void onBuy(p.id)}
              disabled={busy}
              accessibilityRole="button"
              style={({ pressed }) => [styles.primary, (pressed || busy) && styles.dim]}
            >
              <Text style={styles.primaryText}>Buy</Text>
            </Pressable>
          )}
        </View>
      ))}
      {busy && <ActivityIndicator color={colors.forest} />}
      <Pressable
        onPress={() => void onRestore()}
        disabled={busy}
        accessibilityRole="button"
        style={({ pressed }) => [styles.linkRow, (pressed || busy) && styles.dim]}
      >
        <Text style={styles.linkText}>Restore purchases</Text>
      </Pressable>
      {error != null && <Text style={styles.error}>{error}</Text>}
      <Text style={styles.caption}>Billed monthly to your Apple Account. Cancel anytime in iOS Settings.</Text>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: { padding: spacing.xl, gap: spacing.lg },
  title: { ...type.title },
  card: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.md,
  },
  cardText: { flex: 1, gap: spacing.xs },
  name: { fontFamily: fonts.sansSemiBold, fontSize: 17, lineHeight: 22, color: colors.ink },
  price: { ...type.body, color: colors.inkSoft },
  caption: { ...type.caption },
  primary: {
    backgroundColor: colors.forest,
    borderRadius: radii.lg,
    paddingVertical: spacing.sm,
    paddingHorizontal: spacing.lg,
  },
  primaryText: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 22, color: colors.white },
  linkRow: { alignSelf: "flex-start", borderRadius: radii.xs },
  linkText: { fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18, color: colors.forest },
  error: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  dim: { opacity: 0.6 },
});
