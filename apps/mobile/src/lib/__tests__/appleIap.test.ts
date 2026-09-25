import {
  clearInFlight,
  isInFlight,
  linkApplePurchase,
  markInFlight,
  paywallProductIds,
  PurchaseRefused,
  startPurchaseIntent,
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
});
