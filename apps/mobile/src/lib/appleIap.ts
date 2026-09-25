/**
 * Apple In-App Purchase — the two server calls and the product list.
 *
 * Prices are never here: the paywall renders StoreKit's `displayPrice` for the
 * viewer's storefront. Which products exist is decided by the SERVER
 * (/billing/account.apple_iap_product_ids, empty while selling is off), so IAP
 * stays dark in any build until the backend switches it on.
 *
 * The client never decides a purchase is valid: it forwards StoreKit's signed
 * transaction to /billing/apple/link, which verifies it against Apple's root CA.
 * The appAccountToken comes from the server too (purchase-intent), so the
 * binding between a purchase and a workspace has one implementation.
 */
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
