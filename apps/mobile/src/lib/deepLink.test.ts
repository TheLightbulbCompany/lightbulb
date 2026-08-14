import { parseAgentDeepLink, parseCheckoutReturn } from "./deepLink";

describe("parseAgentDeepLink", () => {
  it("parses an agent deep link", () => {
    expect(parseAgentDeepLink("isol8://agent/agent_123")).toEqual({ agentId: "agent_123" });
  });
  it("parses with a query string", () => {
    expect(parseAgentDeepLink("isol8://agent/agent_123?session=s1")).toEqual({ agentId: "agent_123" });
  });
  it("returns null for the oauth callback", () => {
    expect(parseAgentDeepLink("isol8://oauth-native-callback")).toBeNull();
  });
  it("returns null for junk", () => {
    expect(parseAgentDeepLink("https://example.com")).toBeNull();
    expect(parseAgentDeepLink("isol8://agent/")).toBeNull();
  });
  it("returns null (not throws) for malformed percent-encoding", () => {
    expect(parseAgentDeepLink("isol8://agent/%")).toBeNull();
    expect(parseAgentDeepLink("isol8://agent/%zz")).toBeNull();
  });
});

describe("parseCheckoutReturn", () => {
  // Both legs must work: iOS hands back the https universal link when the
  // domain association has resolved, and the fallback page offers the custom
  // scheme when it hasn't.
  it("recognises the universal link and the custom-scheme fallback", () => {
    expect(parseCheckoutReturn("https://isol8.co/checkout_redirect/success")).toEqual({
      status: "success",
    });
    expect(parseCheckoutReturn("isol8://checkout_redirect/success")).toEqual({
      status: "success",
    });
    expect(parseCheckoutReturn("https://isol8.co/checkout_redirect/cancel")).toEqual({
      status: "cancel",
    });
  });

  it("tolerates a trailing query string from Checkout", () => {
    expect(
      parseCheckoutReturn("https://isol8.co/checkout_redirect/success?session_id=cs_1"),
    ).toEqual({ status: "success" });
  });

  // Deep links are externally triggerable. This parser only ever REPORTS a
  // return so the caller can refetch entitlement from the server — but it
  // still must not match a look-alike host, or a hostile link could at least
  // drive UI it has no business driving.
  it("rejects look-alike hosts and unrelated links", () => {
    expect(parseCheckoutReturn("https://isol8.co.evil.com/checkout_redirect/success")).toBeNull();
    expect(parseCheckoutReturn("https://evil.com/checkout_redirect/success")).toBeNull();
    expect(parseCheckoutReturn("isol8://agent/agent_123")).toBeNull();
    expect(parseCheckoutReturn("https://isol8.co/chat")).toBeNull();
    expect(parseCheckoutReturn("")).toBeNull();
  });

  it("rejects an unknown status segment", () => {
    expect(parseCheckoutReturn("https://isol8.co/checkout_redirect/granted")).toBeNull();
  });

// The portal return is a third status: the customer came back from managing
// or cancelling, so the app must refetch entitlement rather than assume.
it("recognises the customer-portal return", () => {
  expect(parseCheckoutReturn("https://isol8.co/checkout_redirect/portal")).toEqual({
    status: "portal",
  });
  expect(parseCheckoutReturn("isol8://checkout_redirect/portal")).toEqual({
    status: "portal",
  });
});
});
