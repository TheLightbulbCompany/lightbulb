import { ApiError } from "@lightbulb/shared";

import {
  SUBSCRIPTION_GATES,
  isPurchaseGate,
  isSubscriptionGate,
  purchaseGateCopy,
  purchaseSafeMessage,
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

// purchaseSafeMessage — the helper every Alert/Text site showing a caught
// ApiError's message routes through (M0's server-402-copy-in-native-Alerts fix).
describe("purchaseSafeMessage", () => {
  function gatedError(code: string, message: string): ApiError {
    return new ApiError(402, { detail: { blocked: true, code, message } }, message);
  }

  const ALL_PURCHASE_CODES = [...PURCHASE, "routines_require_subscription"];

  it.each(ALL_PURCHASE_CODES)(
    "maps a %s error to its local neutral copy, not the server message",
    (code) => {
      const serverMessage = "Upgrade to thelightbulb.company to keep going — pick a plan for $20/mo.";
      const err = gatedError(code, serverMessage);
      const out = purchaseSafeMessage(err, "fallback");
      expect(out).toBe(purchaseGateCopy(code)?.body);
      expect(out).not.toBe(serverMessage);
      // The poisoned server copy (Upgrade / pick a plan / $ / thelightbulb.company)
      // never survives into the sanitized output.
      expect(out).not.toMatch(/upgrade|pick a plan|\$\d/i);
      expect(out).not.toMatch(/thelightbulb\.company/i);
    },
  );

  it("passes through a non-purchase ApiError's own message unchanged", () => {
    const err = new ApiError(404, { detail: "Issue not found" }, "Issue not found");
    expect(purchaseSafeMessage(err, "fallback")).toBe("Issue not found");
  });

  it("falls back for a non-Error value with no body", () => {
    expect(purchaseSafeMessage("not an error", "fallback")).toBe("fallback");
    expect(purchaseSafeMessage(null, "fallback")).toBe("fallback");
  });

  it("falls back to a plain Error's message when there is no structured code", () => {
    expect(purchaseSafeMessage(new Error("network down"), "fallback")).toBe("network down");
  });
});
