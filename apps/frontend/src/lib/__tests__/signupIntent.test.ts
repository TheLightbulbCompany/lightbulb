import { describe, it, expect } from "vitest";
import {
  readSignupIntentCookie,
  resolveSignUpRedirectTarget,
  SIGNUP_INTENT_COOKIE,
} from "../signupIntent";

describe("readSignupIntentCookie", () => {
  it("reads the intent value when present alone", () => {
    expect(readSignupIntentCookie(`${SIGNUP_INTENT_COOKIE}=team`)).toBe("team");
  });

  it("reads the intent value among other cookies", () => {
    expect(
      readSignupIntentCookie(`foo=1; ${SIGNUP_INTENT_COOKIE}=team; bar=2`),
    ).toBe("team");
  });

  it("url-decodes the value", () => {
    expect(readSignupIntentCookie(`${SIGNUP_INTENT_COOKIE}=a%20b`)).toBe("a b");
  });

  it("returns null when the cookie is absent", () => {
    expect(readSignupIntentCookie("foo=1; bar=2")).toBeNull();
  });

  it("returns null for an empty cookie string", () => {
    expect(readSignupIntentCookie("")).toBeNull();
  });

  it("does not match a cookie whose name merely contains the key", () => {
    expect(readSignupIntentCookie(`not_${SIGNUP_INTENT_COOKIE}=team`)).toBeNull();
  });
});

describe("resolveSignUpRedirectTarget", () => {
  it("routes to /onboarding?intent=team on the ?intent=team query alone", () => {
    expect(resolveSignUpRedirectTarget("?intent=team", "")).toBe(
      "/onboarding?intent=team",
    );
  });

  it("routes to /onboarding?intent=team on the cookie alone (query dropped by Clerk's multi-step signup)", () => {
    expect(
      resolveSignUpRedirectTarget("", `${SIGNUP_INTENT_COOKIE}=team`),
    ).toBe("/onboarding?intent=team");
  });

  it("routes to /chat when neither the query nor the cookie carries team intent", () => {
    expect(resolveSignUpRedirectTarget("", "")).toBe("/chat");
    expect(resolveSignUpRedirectTarget("?foo=bar", "other=1")).toBe("/chat");
  });

  it("ignores a query intent value that isn't 'team'", () => {
    expect(resolveSignUpRedirectTarget("?intent=personal", "")).toBe("/chat");
  });
});
