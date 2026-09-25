import { useAuth } from "@clerk/clerk-expo";
import { router, Stack } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { createApiClient } from "@lightbulb/shared";

import { PaywallScreen } from "../../components/billing/PaywallScreen";
import { makeAdapter } from "../../lib/adapter";
import { paywallProductIds } from "../../lib/appleIap";

type Account = {
  plan?: string | null;
  subscription_source?: string | null;
  apple_iap_product_ids?: string[] | null;
  base_price_usd?: number;
  workspace_price_usd?: number;
  ultra_price_usd?: number;
};

/** Inverse of the backend's PRODUCT_PLANS (core/services/apple_iap.py). */
const PLAN_PRODUCTS: Record<string, string> = {
  base: "company.thelightbulb.mobile.base.monthly",
  standard: "company.thelightbulb.mobile.plan.monthly",
  ultra_pro: "company.thelightbulb.mobile.ultra.monthly",
};

export default function PlansScreen() {
  const { getToken } = useAuth();
  // Stable client despite clerk-expo's per-render getToken (see settings.tsx).
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const [account, setAccount] = useState<Account | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get("/billing/account")
      .then((body) => {
        if (!cancelled) setAccount(body as Account);
      })
      // No account → no products → the paywall stays dark.
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api]);

  const currentProductId =
    account?.subscription_source === "apple_iap" && account.plan
      ? (PLAN_PRODUCTS[account.plan] ?? null)
      : null;

  return (
    <>
      <Stack.Screen options={{ title: "Plans" }} />
      <PaywallScreen
        api={api}
        productIds={paywallProductIds(account)}
        currentProductId={currentProductId}
        onPurchased={() => router.back()}
        account={account}
      />
    </>
  );
}
