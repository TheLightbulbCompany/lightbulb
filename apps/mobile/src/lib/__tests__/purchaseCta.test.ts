import { ApiError } from "@lightbulb/shared";

import {
  SUBSCRIPTION_GATES,
  isPurchaseGate,
  isSubscriptionGate,
  purchaseGateCopy,
} from "../purchaseCta";

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
  "included_daily_cap",
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

  // A paying owner at today's included usage: never trial wording, and no plan
  // paywall — they already have a plan.
  it("gives included_daily_cap paid copy, not trial copy", () => {
    const copy = purchaseGateCopy("included_daily_cap");
    expect(`${copy?.title} ${copy?.body}`).not.toMatch(/free|trial/i);
    expect(isSubscriptionGate("included_daily_cap")).toBe(false);
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

// A plan paywall can only resolve gates that mean "no subscription". Two of
// the purchase gates fire for owners who ALREADY pay, and selling those a
// second monthly plan takes a second payment while leaving the gate in place.
describe("which gates a paywall can actually resolve", () => {
  it.each(["trial_daily_cap", "trial_expired", "signup_required", "subscription_required"])(
    "%s means no plan, so the paywall resolves it",
    (code) => {
      expect(isSubscriptionGate(code)).toBe(true);
    },
  );

  it.each(["credits_required", "payment_past_due"])(
    "%s fires for an owner who already pays, so the paywall must NOT show",
    (code) => {
      // Still a purchase gate — the server's copy is a CTA either way — but
      // buying another subscription is not what unblocks them.
      expect(isPurchaseGate(code)).toBe(true);
      expect(isSubscriptionGate(code)).toBe(false);
    },
  );

  it("never shows the personal paywall for an org gate", () => {
    expect(isSubscriptionGate("team_billing_required")).toBe(false);
  });

  it("defaults a new backend gate to NOT showing a paywall", () => {
    expect(isSubscriptionGate("something_new")).toBe(false);
    expect(isSubscriptionGate(null)).toBe(false);
  });

  it("only classifies gates it also carries local copy for", () => {
    for (const code of SUBSCRIPTION_GATES) {
      expect(purchaseGateCopy(code)).not.toBeNull();
    }
  });
});

