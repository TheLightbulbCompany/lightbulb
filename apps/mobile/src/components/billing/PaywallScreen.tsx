/**
 * Workspace subscription handoff for the mobile app.
 *
 * New StoreKit purchase, restore, and link flows are retired. The only purchase
 * path is the existing workspace Checkout opened in the device's default
 * browser. The external option remains U.S.-storefront-only; elsewhere the app
 * stays a read-only companion under App Store guideline 3.1.3(f).
 */
import { useEffect, useState } from "react";
import { Linking, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { getStorefront, useIAP } from "expo-iap";

import { WEB_BILLING_URL } from "../../config/env";
import { colors, radii, spacing, type } from "../../config/theme";
import { parseCheckoutReturn } from "../../lib/deepLink";

const EXTERNAL_LINK_STOREFRONTS = new Set(["USA"]);

export function PaywallScreen({
  api,
  onPurchased,
}: {
  api: {
    get: (path: string) => Promise<unknown>;
    post: (path: string, body: unknown) => Promise<unknown>;
  };
  onPurchased: () => void;
}) {
  const { connected } = useIAP();
  const [storefront, setStorefront] = useState<string>();
  const [webPriceUsd, setWebPriceUsd] = useState<number | null>(null);
  const [webBusy, setWebBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    void getStorefront()
      .then((code: string) => {
        if (!cancelled) setStorefront(code);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [connected]);

  useEffect(() => {
    let cancelled = false;
    void api
      .get("/billing/account")
      .then((body) => {
        const price = (body as { workspace_price_usd?: number })?.workspace_price_usd;
        if (!cancelled && typeof price === "number") setWebPriceUsd(price);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api]);

  useEffect(() => {
    const sub = Linking.addEventListener("url", ({ url }) => {
      if (parseCheckoutReturn(url)?.status === "success") onPurchased();
    });
    return () => sub.remove();
  }, [onPurchased]);

  async function onWebCheckout() {
    if (webBusy) return;
    setWebBusy(true);
    setError(null);
    try {
      const body = (await api.post("/billing/checkout", { plan: "workspace" })) as {
        checkout_url?: string;
      };
      const url = typeof body.checkout_url === "string" ? body.checkout_url : null;
      await Linking.openURL(url ?? WEB_BILLING_URL);
    } catch {
      await Linking.openURL(WEB_BILLING_URL).catch(() => {
        setError("Couldn't open checkout. Try again in a moment.");
      });
    } finally {
      setWebBusy(false);
    }
  }

  return (
    <ScrollView style={styles.page} contentContainerStyle={styles.content} testID="paywall">
      <Text style={styles.title}>Lightbulb</Text>
      <Text style={styles.lede}>
        Keep your agents running around the clock, with a higher monthly compute allowance.
      </Text>

      {storefront != null && EXTERNAL_LINK_STOREFRONTS.has(storefront) && (
        <View style={styles.card}>
          <Text style={styles.overline}>SUBSCRIBE ON THE WEB</Text>
          <Text style={styles.caption}>
            {webPriceUsd != null
              ? `Subscribe to your workspace on flickmeon.ai for $${webPriceUsd}/month, and manage billing there.`
              : "Subscribe to your workspace on flickmeon.ai, and manage billing there."}
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
  title: { ...type.display, textAlign: "center" },
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
  caption: { ...type.caption },
  linkRow: { alignSelf: "flex-start", borderRadius: radii.xs },
  linkText: { ...type.body, color: colors.forest },
  error: { ...type.caption, color: colors.danger, textAlign: "center" },
  dim: { opacity: 0.55 },
});
