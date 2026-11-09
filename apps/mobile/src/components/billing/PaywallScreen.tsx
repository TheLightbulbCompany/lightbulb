/**
 * In-app subscription purchase (App Store guideline 3.1.1).
 *
 * Which products exist is the SERVER's call (`productIds`, from
 * /billing/account); prices are StoreKit's `displayPrice` for the viewer's
 * storefront.
 *
 * Under each Buy sits a smaller "Or pay $X/month on the web": Stripe Checkout
 * in the browser (the app is US-only, where that link is allowed). Stripe's
 * webhook grants that plan server-side; the return link and coming back to the
 * app only REFETCH (`onPurchased`) — a URL anyone can construct grants nothing.
 *
 * The purchase is not trusted here. StoreKit's signed transaction goes to the
 * backend, which verifies it against Apple's root CA; only that call grants a
 * plan. `finishTransaction` runs strictly AFTER the server confirms, so a
 * failure or crash mid-flow leaves the transaction unfinished and StoreKit
 * redelivers it on the next launch instead of losing a paid purchase.
 *
 * The launch-time PurchaseListener also receives every StoreKit delivery. The
 * two split them by time: while this screen is buying or restoring, it marks
 * every offered product id in flight (lib/appleIap) and the listener skips
 * them. Mid-buy this screen links ANY offered product's delivery — a crossgrade
 * can arrive as another SKU, and the server converges on the original
 * transaction id, so that is harmless. While idle it leaves every delivery (a
 * renewal, an Ask to Buy approval) to the listener.
 */
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  AppState,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { ErrorCode, getAvailablePurchases } from "expo-iap";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import {
  type ApiPost,
  clearInFlight,
  linkApplePurchase,
  markInFlight,
  PLAN_ORDER,
  PurchaseRefused,
  startPurchaseIntent,
  startWebCheckout,
  webPriceFor,
} from "../../lib/appleIap";
import { parseCheckoutReturn } from "../../lib/deepLink";
import { useSharedIAP } from "./IapProvider";

const FALLBACK_TITLES: Record<string, string> = {
  "company.thelightbulb.mobile.base.monthly": "Base",
  "company.thelightbulb.mobile.plan.monthly": "Team",
  "company.thelightbulb.mobile.ultra.monthly": "Ultra",
};

const WEB_CHECKOUT_BLOCKED = "A checkout is already open, or this workspace is billed through the App Store.";
const WEB_CHECKOUT_FAILED = "Couldn't open checkout. Try again in a moment.";

const LINK_PENDING =
  "Your purchase went through, but we couldn't activate it yet. It will finish the next time you open the app.";
const LINK_REFUSED =
  "We couldn't apply this purchase to this workspace. Contact support@thelightbulb.company.";
const LINK_STRIPE_MANAGED =
  "This workspace is already billed another way, so this App Store purchase wasn't applied. Contact support@thelightbulb.company for a refund.";

/** Same wording as the web: a live plan change is prorated onto the next invoice. */
function confirmPlanChange(name: string, price: number | null): Promise<boolean> {
  return new Promise((resolve) =>
    Alert.alert(
      "Change plan?",
      `Switch to ${name}${price === null ? "" : ` ($${price}/month)`}? Your plan changes now. ` +
        "The prorated difference for the rest of this billing period is added to your next invoice.",
      [
        { text: "Cancel", style: "cancel", onPress: () => resolve(false) },
        { text: "Change plan", onPress: () => resolve(true) },
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    ),
  );
}

/** The one plan's price line (T06), from the server's prices; null until they load. */
function planLine(account: Parameters<typeof webPriceFor>[1] | undefined): string | null {
  const { workspace_price_usd: price, seat_price_usd: seat, included_members: included } = account ?? {};
  if (price == null || seat == null || included == null) return null;
  return `$${price} a month for up to ${included} people. $${seat} a month for each member from the ${included + 1}th.`;
}

type Purchase = { purchaseToken?: string | null; productId?: string; id?: string };

/** Copy for a failed link. Every case leaves the transaction unfinished. */
function linkErrorCopy(e: unknown): string {
  const err = e as { status?: number; body?: { detail?: string } } | null;
  // Stripe started billing between intent and link.
  if (err?.status === 409 && err.body?.detail === "stripe_managed") return LINK_STRIPE_MANAGED;
  // The server refused this purchase for this workspace: retrying won't help.
  if (err?.status === 400 || err?.status === 403) return LINK_REFUSED;
  return LINK_PENDING; // Network / 5xx: StoreKit redelivers next launch.
}

export function PaywallScreen({
  api,
  productIds,
  currentProductId,
  onPurchased,
  fallback = null,
  footer = null,
  account = null,
  onWebReturn = onPurchased,
}: {
  /** Authenticated client — injected so this stays testable without Clerk. */
  api: { get: (path: string) => Promise<unknown>; post: ApiPost };
  productIds: string[];
  currentProductId: string | null;
  onPurchased: () => void;
  /** Shown instead while StoreKit has nothing to show (loading, or none of the products). */
  fallback?: ReactNode;
  /** Rendered under the paywall's own content. */
  footer?: ReactNode;
  /** The /billing/account body: its *_price_usd fields price the web option. */
  account?: Parameters<typeof webPriceFor>[1];
  /** Refetch after a web checkout return (may be abandoned). Defaults to onPurchased. */
  onWebReturn?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  /** True between a Buy tap and its outcome: only then are deliveries ours. */
  const buying = useRef(false);
  /** Product ids marked in flight for the listener to skip. */
  const claimed = useRef<string[]>([]);

  const release = useCallback(() => {
    claimed.current.forEach(clearInFlight);
    claimed.current = [];
    buying.current = false;
  }, []);
  // Unmounted mid-purchase: hand any late delivery back to the listener.
  useEffect(() => release, [release]);

  const setBusyBoth = useCallback(
    (v: boolean, ids: string[] = []) => {
      busyRef.current = v;
      setBusy(v);
      if (v) {
        claimed.current = ids;
        ids.forEach(markInFlight);
      } else {
        release();
      }
    },
    [release],
  );

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
      // Not mid-buy, or not one of our plans: PurchaseListener owns it.
      if (!buying.current || !productIds.includes(purchase.productId ?? "")) return;
      void (async () => {
        try {
          if (!purchase.purchaseToken) {
            setError("That purchase couldn't be confirmed. Contact support@thelightbulb.company.");
            return;
          }
          await linkAndFinish({ ...purchase, purchaseToken: purchase.purchaseToken });
          onPurchased();
        } catch (e) {
          setError(linkErrorCopy(e));
        } finally {
          setBusyBoth(false);
        }
      })();
    },
    [linkAndFinish, onPurchased, productIds, setBusyBoth],
  );

  const { connected, subscriptions, fetchProducts, requestPurchase, finishTransaction, restorePurchases } =
    useSharedIAP({
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

  // A web checkout completes in the browser and Stripe's webhook grants it, so
  // coming back — by the return link, or just switching back — only refetches.
  const webCheckoutOpened = useRef(false);
  useEffect(() => {
    const url = Linking.addEventListener("url", ({ url }) => {
      if (parseCheckoutReturn(url)?.status === "success") onWebReturn();
    });
    const appState = AppState.addEventListener("change", (state) => {
      if (state === "active" && webCheckoutOpened.current) {
        webCheckoutOpened.current = false;
        onWebReturn();
      }
    });
    return () => {
      url.remove();
      appState.remove();
    };
  }, [onWebReturn]);

  const skuKey = productIds.join(",");
  useEffect(() => {
    if (!connected || skuKey === "") return;
    void fetchProducts({ skus: skuKey.split(","), type: "subs" });
  }, [connected, fetchProducts, skuKey]);

  const products = subscriptions
    .filter((s) => productIds.includes(s.id))
    .sort((a, b) => (PLAN_ORDER[a.id] ?? 99) - (PLAN_ORDER[b.id] ?? 99));

  if (productIds.length === 0 || products.length === 0) return <>{fallback}</>;

  async function onBuy(sku: string) {
    if (busyRef.current) return;
    setBusyBoth(true, productIds);
    buying.current = true;
    setError(null);
    try {
      const { appAccountToken } = await startPurchaseIntent(api.post);
      await requestPurchase({ type: "subs", request: { apple: { sku, appAccountToken } } });
    } catch (e) {
      setBusyBoth(false);
      setError(
        e instanceof PurchaseRefused && e.reason === "stripe_managed"
          ? "This workspace is billed outside the App Store."
          : e instanceof PurchaseRefused && e.reason === "not_admin"
            ? "Only a workspace admin can choose a plan."
            : "Couldn't start the purchase. Try again in a moment.",
      );
    }
  }

  async function onWebCheckout(sku: string) {
    if (busyRef.current) return;
    setBusyBoth(true);
    setError(null);
    try {
      let result = await startWebCheckout(api.post, sku);
      if (result.confirm_required) {
        if (!(await confirmPlanChange(FALLBACK_TITLES[sku] ?? "this plan", webPriceFor(sku, account)))) return;
        result = await startWebCheckout(api.post, sku, true);
      }
      if (result.checkout_url) {
        webCheckoutOpened.current = true;
        await Linking.openURL(result.checkout_url);
      } else if (result.updated) {
        onWebReturn(); // swapped in place: refetch the account
      }
    } catch (e) {
      webCheckoutOpened.current = false;
      const err = e as { status?: number; body?: { detail?: string } } | null;
      const detail = err?.body?.detail;
      setError(
        err?.status === 409 && (detail === "apple_managed" || detail === "checkout_in_progress")
          ? WEB_CHECKOUT_BLOCKED
          : WEB_CHECKOUT_FAILED,
      );
    } finally {
      setBusyBoth(false);
    }
  }

  async function onRestore() {
    if (busyRef.current) return;
    setBusyBoth(true, productIds);
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
      else if (found.length > 0) setError(linkErrorCopy(linkError));
      else setError("No previous purchase found for this Apple Account.");
    } catch {
      setError("Couldn't restore purchases. Try again in a moment.");
    } finally {
      setBusyBoth(false);
    }
  }

  return (
    <ScrollView style={styles.page} contentContainerStyle={styles.content} testID="paywall">
      <Text style={styles.title}>Keep your team on Lightbulb</Text>
      {planLine(account) != null && <Text style={styles.price}>{planLine(account)}</Text>}
      <Text style={styles.caption}>
        Includes your six AI employees, their memory and email, the shared browser, the task board and the always-on
        runtime. AI usage: sign in with your ChatGPT account, or use Lightbulb credits at the
        model&apos;s price plus 20%.
      </Text>
      {products.map((p) => {
        const name = p.title || FALLBACK_TITLES[p.id] || p.id;
        const webPrice = webPriceFor(p.id, account);
        const webLabel = `Or pay $${webPrice}/month on the web`;
        return (
          <View key={p.id} style={styles.card}>
            <View style={styles.cardRow}>
              <View style={styles.cardText}>
                <Text style={styles.name}>{name}</Text>
                <Text style={styles.price}>{`${p.displayPrice}/month`}</Text>
              </View>
              {p.id === currentProductId ? (
                <Text style={styles.caption}>Current plan</Text>
              ) : (
                <Pressable
                  onPress={() => void onBuy(p.id)}
                  disabled={busy}
                  accessibilityRole="button"
                  accessibilityLabel={`Buy ${name}`}
                  style={({ pressed }) => [styles.primary, (pressed || busy) && styles.dim]}
                >
                  <Text style={styles.primaryText}>Buy</Text>
                </Pressable>
              )}
            </View>
            {p.id !== currentProductId && webPrice != null && (
              <Pressable
                onPress={() => void onWebCheckout(p.id)}
                disabled={busy}
                accessibilityRole="button"
                accessibilityLabel={webLabel}
                style={({ pressed }) => [styles.webRow, (pressed || busy) && styles.dim]}
              >
                <Text style={styles.webText}>{webLabel}</Text>
              </Pressable>
            )}
          </View>
        );
      })}
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
      {footer}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: { padding: spacing.xl, gap: spacing.lg },
  title: { ...type.title },
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.md,
  },
  cardRow: { flexDirection: "row", alignItems: "center", gap: spacing.md },
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
  webRow: { alignSelf: "flex-end", borderRadius: radii.xs },
  webText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.inkSoft },
  error: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  dim: { opacity: 0.6 },
});
