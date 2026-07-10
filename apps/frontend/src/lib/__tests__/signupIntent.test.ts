import { describe, it, expect } from "vitest";
import { readSignupIntentCookie, SIGNUP_INTENT_COOKIE } from "../signupIntent";

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
