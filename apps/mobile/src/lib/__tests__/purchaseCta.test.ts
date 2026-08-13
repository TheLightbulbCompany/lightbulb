import { isPurchaseGate, purchaseGateCopy } from "../purchaseCta";

// The full gate-code list from apps/backend/core/services/provision_gate.py
// (grep `code="`). Pinned here so adding a backend gate that costs money and
// forgetting to classify it fails this test rather than shipping a purchase
// CTA to iOS — which is what got 0.1.0 (17) rejected.
const PURCHASE = [
  "trial_daily_cap",
  "trial_expired",
  "signup_required",
  "subscription_required",
  "payment_past_due",
  "team_billing_required",
  "credits_required",
];

const NOT_PURCHASE = [
  "invitation_pending",
  "provider_required",
  "oauth_required",
  "byo_provider_required",
];

describe("purchaseCta", () => {
  it.each(PURCHASE)("classifies %s as a purchase gate", (code) => {
    expect(isPurchaseGate(code)).toBe(true);
    expect(purchaseGateCopy(code)).not.toBeNull();
  });

  it.each(NOT_PURCHASE)("leaves %s alone", (code) => {
    expect(isPurchaseGate(code)).toBe(false);
    expect(purchaseGateCopy(code)).toBeNull();
  });

  it("treats an unknown or absent code as not-a-purchase-gate", () => {
    expect(isPurchaseGate("something_new")).toBe(false);
    expect(isPurchaseGate(undefined)).toBe(false);
    expect(isPurchaseGate(null)).toBe(false);
    expect(purchaseGateCopy(undefined)).toBeNull();
  });

  // The replacement copy exists to REMOVE the instruction to buy. Copy that
  // reintroduces one would defeat the whole module.
  it.each(PURCHASE)("gives %s copy with no call to action", (code) => {
    const copy = purchaseGateCopy(code);
    const text = `${copy?.title} ${copy?.body}`;
    expect(text).not.toMatch(/upgrade|subscribe|buy|purchase|top up|pick a plan/i);
    expect(text).not.toMatch(/isol8\.co/);
  });
});
