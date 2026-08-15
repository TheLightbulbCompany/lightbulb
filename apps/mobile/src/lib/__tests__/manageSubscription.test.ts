import { Linking } from "react-native";

import { isAppleManaged, openSubscriptionManagement } from "../manageSubscription";

// The two billing systems cannot see each other's subscriptions. Sending a
// subscriber to the wrong one shows them "no subscriptions", from which the
// reasonable conclusion is that they already cancelled — so they stop trying,
// keep getting charged, and open a chargeback rather than a support ticket.
// That is why this routing has its own tests.
describe("isAppleManaged", () => {
  it("is true only for the Apple source", () => {
    expect(isAppleManaged("apple_iap")).toBe(true);
    expect(isAppleManaged("stripe")).toBe(false);
  });

  // Every row predating Apple IAP carries no source at all, and all of those
  // are Stripe. Defaulting the unknown case to Apple would send the entire
  // existing subscriber base to iOS Settings, where none of them appear.
  it("treats an absent source as Stripe", () => {
    expect(isAppleManaged(null)).toBe(false);
    expect(isAppleManaged(undefined)).toBe(false);
    expect(isAppleManaged("")).toBe(false);
  });
});

describe("openSubscriptionManagement", () => {
  it("sends an Apple subscriber to iOS Settings, never to the Stripe portal", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(undefined as never);
    const openApple = jest.fn(async () => {});

    await openSubscriptionManagement({
      source: "apple_iap",
      // Even with a portal URL in hand, Apple wins — Stripe has no record of
      // this subscription, so the portal would be a dead end.
      portalUrl: "https://billing.stripe.com/p/session_123",
      openAppleSubscriptions: openApple,
    });

    expect(openApple).toHaveBeenCalled();
    expect(openURL).not.toHaveBeenCalled();
    openURL.mockRestore();
  });

  it("sends a Stripe subscriber to the portal, never to iOS Settings", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(undefined as never);
    const openApple = jest.fn(async () => {});

    await openSubscriptionManagement({
      source: null,
      portalUrl: "https://billing.stripe.com/p/session_123",
      openAppleSubscriptions: openApple,
    });

    expect(openURL).toHaveBeenCalledWith("https://billing.stripe.com/p/session_123");
    expect(openApple).not.toHaveBeenCalled();
    openURL.mockRestore();
  });

  // A portal outage must not leave someone trying to cancel on a dead button.
  it("falls back to the billing page when no portal session could be created", async () => {
    const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(undefined as never);

    await openSubscriptionManagement({
      source: null,
      portalUrl: null,
      openAppleSubscriptions: jest.fn(async () => {}),
    });

    expect(openURL).toHaveBeenCalledWith("https://isol8.co/settings?panel=billing");
    openURL.mockRestore();
  });
});
