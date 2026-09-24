import { isAppleManaged } from "../manageSubscription";

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
