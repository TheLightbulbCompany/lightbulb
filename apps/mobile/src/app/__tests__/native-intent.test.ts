import { redirectSystemPath } from "../+native-intent";

// The app claims /checkout_redirect/* via associated domains so Stripe can hand
// control back after a web purchase. There is no route by that name, so without
// a rewrite the customer lands on the unmatched-route screen immediately after
// paying — and App Review hits it too if they use the web option.
describe("checkout return links", () => {
  it.each([
    "/checkout_redirect/connected",
    "checkout_redirect/connect_failed?error=oauth_provider",
    "flickmeon://checkout_redirect/connected",
    "https://thelightbulb.company/checkout_redirect/connect_failed",
  ])("keeps the current room on a warm OAuth return %s", (path) => {
    expect(redirectSystemPath({ path, initial: false })).toBeNull();
    expect(redirectSystemPath({ path, initial: true })).toBe("/");
  });

  it.each(["/checkout_redirect/success", "/checkout_redirect/cancel", "/checkout_redirect/portal"])(
    "sends %s to the app entry point instead of a dead route",
    (path) => {
      expect(redirectSystemPath({ path, initial: true })).toBe("/");
    },
  );

  it("handles the path without a leading slash", () => {
    expect(redirectSystemPath({ path: "checkout_redirect/success", initial: false })).toBe("/");
  });

  it("leaves every other deep link alone", () => {
    expect(redirectSystemPath({ path: "/plans", initial: false })).toBe("/plans");
    // Guards the regex boundary: a route that merely starts with the same
    // letters is a real destination and must still resolve.
    expect(redirectSystemPath({ path: "/checkout_redirection", initial: false })).toBe(
      "/checkout_redirection",
    );
  });
});

describe("links to the removed chat app", () => {
  it.each(["/(app)/task/42", "flickmeon://agent/agent_1", "/(app)/room/r1/details", "/session/s1", "/workspaces", "control/llm"])(
    "sends %s to Home",
    (path) => expect(redirectSystemPath({ path, initial: true })).toBe("/"),
  );
  it("keeps the chat screen", () => {
    expect(redirectSystemPath({ path: "/(app)/room/r1", initial: true })).toBe("/(app)/room/r1");
  });
  it("keeps a route that only starts with the same letters", () => {
    expect(redirectSystemPath({ path: "/worker", initial: false })).toBe("/worker");
  });
});
