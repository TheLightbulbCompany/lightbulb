import { describe, it, expect } from "vitest";
import { signupRedirectUrl } from "../signupRedirect";

describe("signupRedirectUrl", () => {
  it("carries team intent into the post-signup redirect", () => {
    expect(signupRedirectUrl("team")).toBe("/chat?intent=team");
  });

  it("defaults to /chat for a personal signup (no intent)", () => {
    expect(signupRedirectUrl(undefined)).toBe("/chat");
  });

  it("ignores unrecognized intent values", () => {
    expect(signupRedirectUrl("bogus")).toBe("/chat");
  });
});
