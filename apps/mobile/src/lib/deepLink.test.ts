import { parseCheckoutReturn } from "./deepLink";

describe("parseCheckoutReturn", () => {
  // Both legs must work: iOS hands back the https universal link when the
  // domain association has resolved, and the fallback page offers the custom
  // scheme when it hasn't.
  it("recognises the universal link and the custom-scheme fallback", () => {
    expect(parseCheckoutReturn("https://thelightbulb.company/checkout_redirect/success")).toEqual({
      status: "success",
    });
    expect(parseCheckoutReturn("flickmeon://checkout_redirect/success")).toEqual({
      status: "success",
    });
    expect(parseCheckoutReturn("https://thelightbulb.company/checkout_redirect/cancel")).toEqual({
      status: "cancel",
    });
  });

  it("tolerates a trailing query string from Checkout", () => {
    expect(
      parseCheckoutReturn("https://thelightbulb.company/checkout_redirect/success?session_id=cs_1"),
    ).toEqual({ status: "success" });
  });

  // Deep links are externally triggerable. This parser only ever REPORTS a
  // return so the caller can refetch entitlement from the server — but it
  // still must not match a look-alike host, or a hostile link could at least
  // drive UI it has no business driving.
  it("rejects look-alike hosts and unrelated links", () => {
    expect(parseCheckoutReturn("https://thelightbulb.company.evil.com/checkout_redirect/success")).toBeNull();
    expect(parseCheckoutReturn("https://evil.com/checkout_redirect/success")).toBeNull();
    expect(parseCheckoutReturn("flickmeon://agent/agent_123")).toBeNull();
    expect(parseCheckoutReturn("https://thelightbulb.company/chat")).toBeNull();
    expect(parseCheckoutReturn("")).toBeNull();
  });

  it("rejects an unknown status segment", () => {
    expect(parseCheckoutReturn("https://thelightbulb.company/checkout_redirect/granted")).toBeNull();
  });

// The portal return is a third status: the customer came back from managing
// or cancelling, so the app must refetch entitlement rather than assume.
it("recognises the customer-portal return", () => {
  expect(parseCheckoutReturn("https://thelightbulb.company/checkout_redirect/portal")).toEqual({
    status: "portal",
  });
  expect(parseCheckoutReturn("flickmeon://checkout_redirect/portal")).toEqual({
    status: "portal",
  });
});
});
