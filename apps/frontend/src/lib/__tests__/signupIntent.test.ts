import { describe, it, expect, beforeEach } from "vitest";
import {
  hasTeamSignupIntentCookie,
  readSignupIntentCookie,
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

/**
 * The single team-intent predicate. `useOnboardingGate` routes on it and
 * `useProvisioningState` suppresses the provisioning poll on it, so its
 * false-on-failure contract is load-bearing in both directions: a personal
 * signup must not be mis-tagged as a team, and a broken cookie must not leave
 * anyone unable to provision.
 */
describe("hasTeamSignupIntentCookie", () => {
  beforeEach(() => {
    // jsdom persists document.cookie across tests in a file.
    document.cookie = `${SIGNUP_INTENT_COOKIE}=; max-age=0; path=/`;
  });

  it("is true when the cookie says team", () => {
    document.cookie = `${SIGNUP_INTENT_COOKIE}=team; path=/`;
    expect(hasTeamSignupIntentCookie()).toBe(true);
  });

  it("is false when the cookie is absent", () => {
    expect(hasTeamSignupIntentCookie()).toBe(false);
  });

  it("is false for a non-team intent value", () => {
    document.cookie = `${SIGNUP_INTENT_COOKIE}=personal; path=/`;
    expect(hasTeamSignupIntentCookie()).toBe(false);
  });

  it("fails OPEN on an undecodable value instead of throwing during render", () => {
    // `decodeURIComponent("%")` raises URIError. Both callers read this during
    // render, so an escaping throw would crash the chat shell.
    document.cookie = `${SIGNUP_INTENT_COOKIE}=%; path=/`;
    expect(() => hasTeamSignupIntentCookie()).not.toThrow();
    expect(hasTeamSignupIntentCookie()).toBe(false);
  });
});
