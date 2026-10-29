/**
 * Apple In-App Purchase — the two server calls and the product list.
 *
 * App Store prices are never here: the paywall renders StoreKit's `displayPrice`
 * for the viewer's storefront. The web (Stripe) alternative's price is the
 * server's, from /billing/account. Which products exist is decided by the SERVER
 * (/billing/account.apple_iap_product_ids, empty while selling is off), so IAP
 * stays dark in any build until the backend switches it on.
 *
 * The client never decides a purchase is valid: it forwards StoreKit's signed
 * transaction to /billing/apple/link, which verifies it against Apple's root CA.
 * The appAccountToken comes from the server too (purchase-intent), so the
 * binding between a purchase and a workspace has one implementation.
 */
import { Platform } from "react-native";

export type ApiPost = (path: string, body: unknown) => Promise<unknown>;
export type PurchaseIntent = { appAccountToken: string; productIds: string[] };

export const PLAN_ORDER: Record<string, number> = {
  "company.thelightbulb.mobile.base.monthly": 0,
  "company.thelightbulb.mobile.plan.monthly": 1,
  "company.thelightbulb.mobile.ultra.monthly": 2,
};

export function paywallProductIds(
  account: { apple_iap_product_ids?: string[] | null } | null | undefined,
): string[] {
  // App Store products only: Android sells nothing in-app, so every paywall
  // shows its no-products fallback there, as iOS does while selling is off.
  if (Platform.OS !== "ios") return [];
  const ids = account?.apple_iap_product_ids ?? [];
  return [...ids].sort((a, b) => (PLAN_ORDER[a] ?? 99) - (PLAN_ORDER[b] ?? 99));
}

export class PurchaseRefused extends Error {
  constructor(public reason: "stripe_managed" | "not_admin" | "unavailable") {
    super(reason);
  }
}

export async function startPurchaseIntent(post: ApiPost): Promise<PurchaseIntent> {
  try {
    const body = (await post("/billing/apple/purchase-intent", {})) as {
      app_account_token: string;
      product_ids: string[];
    };
    return { appAccountToken: body.app_account_token, productIds: body.product_ids };
  } catch (e) {
    const status = (e as { status?: number }).status;
    const detail = (e as { body?: { detail?: string } }).body?.detail;
    if (status === 409 && detail === "stripe_managed") throw new PurchaseRefused("stripe_managed");
    if (status === 403) throw new PurchaseRefused("not_admin");
    if (status === 404) throw new PurchaseRefused("unavailable");
    throw e;
  }
}

export async function linkApplePurchase(
  post: ApiPost,
  signedTransaction: string,
): Promise<{ plan: string; status: string }> {
  return (await post("/billing/apple/link", { signed_transaction: signedTransaction })) as {
    plan: string;
    status: string;
  };
}

/** The Stripe plan each App Store product is also sold as on the web. */
export const WEB_PLAN_FOR_PRODUCT: Record<string, "base" | "workspace" | "ultra"> = {
  "company.thelightbulb.mobile.base.monthly": "base",
  "company.thelightbulb.mobile.plan.monthly": "workspace",
  "company.thelightbulb.mobile.ultra.monthly": "ultra",
};

type WebPrices = {
  base_price_usd?: number;
  workspace_price_usd?: number;
  ultra_price_usd?: number;
  seat_price_usd?: number;
  included_members?: number;
  subscription_source?: string | null;
};

/**
 * The server's web (Stripe) monthly price for a product's plan, or null if
 * unknown — or if the App Store bills this workspace (Checkout 409s those).
 */
export function webPriceFor(productId: string, account: WebPrices | null): number | null {
  if (account?.subscription_source === "apple_iap") return null;
  const plan = WEB_PLAN_FOR_PRODUCT[productId];
  return (plan && account?.[`${plan}_price_usd` as const]) ?? null;
}

export type WebCheckoutResult = { checkout_url?: string; updated?: boolean; confirm_required?: boolean };

/**
 * Start the web (Stripe) plan purchase, returning to the app. A workspace with
 * no subscription gets a Checkout `checkout_url`. A workspace that already pays
 * gets `confirm_required` until it resends with `confirm`, then `updated`:
 * the plan is swapped in place and there is no URL to open.
 */
export async function startWebCheckout(
  post: ApiPost,
  productId: string,
  confirm = false,
): Promise<WebCheckoutResult> {
  return (await post("/billing/checkout", {
    plan: WEB_PLAN_FOR_PRODUCT[productId],
    return_to: "app",
    confirm,
  })) as WebCheckoutResult;
}

/**
 * Product ids the paywall is buying or restoring right now. Both the paywall
 * and the launch-time PurchaseListener receive `onPurchaseSuccess`; the
 * listener skips a product in here so one purchase is never linked twice.
 */
const inFlight = new Set<string>();
export const markInFlight = (id: string) => void inFlight.add(id);
export const clearInFlight = (id: string) => void inFlight.delete(id);
export const isInFlight = (id: string) => inFlight.has(id);
