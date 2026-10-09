import {
  clearInFlight,
  isInFlight,
  linkApplePurchase,
  markInFlight,
  paywallProductIds,
  PurchaseRefused,
  startPurchaseIntent,
  startWebCheckout,
  webPriceFor,
} from "../appleIap";

describe("appleIap", () => {
  it("offers nothing when the server lists no products", () => {
    expect(paywallProductIds(null)).toEqual([]);
    expect(paywallProductIds({ apple_iap_product_ids: [] })).toEqual([]);
  });

  it("orders products Base, Workspace, Ultra", () => {
    expect(
      paywallProductIds({
        apple_iap_product_ids: [
          "company.thelightbulb.mobile.ultra.monthly",
          "company.thelightbulb.mobile.base.monthly",
          "company.thelightbulb.mobile.plan.monthly",
        ],
      }),
    ).toEqual([
      "company.thelightbulb.mobile.base.monthly",
      "company.thelightbulb.mobile.plan.monthly",
      "company.thelightbulb.mobile.ultra.monthly",
    ]);
  });

  it("maps a 409 stripe_managed intent to a refusal", async () => {
    const err = Object.assign(new Error("API 409"), { status: 409, body: { detail: "stripe_managed" } });
    await expect(startPurchaseIntent(jest.fn().mockRejectedValue(err))).rejects.toMatchObject({
      reason: "stripe_managed",
    });
    await expect(startPurchaseIntent(jest.fn().mockRejectedValue(err))).rejects.toBeInstanceOf(PurchaseRefused);
  });

  it("returns the server token", async () => {
    const post = jest.fn().mockResolvedValue({ app_account_token: "t", product_ids: ["a"] });
    await expect(startPurchaseIntent(post)).resolves.toEqual({ appAccountToken: "t", productIds: ["a"] });
    expect(post).toHaveBeenCalledWith("/billing/apple/purchase-intent", {});
  });

  it("links a signed transaction", async () => {
    const post = jest.fn().mockResolvedValue({ plan: "base", status: "active" });
    await expect(linkApplePurchase(post, "jws")).resolves.toEqual({ plan: "base", status: "active" });
    expect(post).toHaveBeenCalledWith("/billing/apple/link", { signed_transaction: "jws" });
  });

  it("tracks in-flight product ids", () => {
    expect(isInFlight("t1")).toBe(false);
    markInFlight("t1");
    expect(isInFlight("t1")).toBe(true);
    expect(isInFlight("t2")).toBe(false);
    clearInFlight("t1");
    expect(isInFlight("t1")).toBe(false);
  });

  it("no web price when the App Store bills the workspace", () => {
    expect(webPriceFor("company.thelightbulb.mobile.base.monthly", { base_price_usd: 20, subscription_source: "apple_iap" })).toBeNull();
  });

  it("prices each plan's web option from /billing/account", () => {
    const account = { base_price_usd: 20, workspace_price_usd: 50, ultra_price_usd: 150 };
    expect(webPriceFor("company.thelightbulb.mobile.base.monthly", account)).toBe(20);
    expect(webPriceFor("company.thelightbulb.mobile.plan.monthly", account)).toBe(50);
    expect(webPriceFor("company.thelightbulb.mobile.ultra.monthly", account)).toBe(150);
    expect(webPriceFor("unknown", account)).toBeNull();
    expect(webPriceFor("company.thelightbulb.mobile.base.monthly", null)).toBeNull();
    expect(webPriceFor("company.thelightbulb.mobile.base.monthly", {})).toBeNull();
  });

  it("starts a web checkout for the product's plan, returning to the app", async () => {
    const post = jest.fn().mockResolvedValue({ checkout_url: "https://checkout.stripe.com/c/x" });
    await expect(startWebCheckout(post, "company.thelightbulb.mobile.plan.monthly")).resolves.toEqual({
      checkout_url: "https://checkout.stripe.com/c/x",
    });
    expect(post).toHaveBeenCalledWith("/billing/checkout", { plan: "workspace", return_to: "app", confirm: false });
  });

  it("passes the plan-change confirmation and returns the server's answer as is", async () => {
    const post = jest.fn().mockResolvedValue({ updated: true });
    await expect(startWebCheckout(post, "company.thelightbulb.mobile.ultra.monthly", true)).resolves.toEqual({
      updated: true,
    });
    expect(post).toHaveBeenCalledWith("/billing/checkout", { plan: "ultra", return_to: "app", confirm: true });
  });
});
