// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from "vitest";
import { render } from "@testing-library/react";

import { SignupIntentCookie } from "../SignupIntentCookie";
import { SIGNUP_INTENT_COOKIE } from "@/lib/signupIntent";

describe("SignupIntentCookie", () => {
  beforeEach(() => {
    document.cookie = `${SIGNUP_INTENT_COOKIE}=; max-age=0; path=/`;
    window.history.replaceState({}, "", "/sign-up");
  });

  it("writes the team cookie when the page is opened with ?intent=team directly", () => {
    window.history.replaceState({}, "", "/sign-up?intent=team");
    render(<SignupIntentCookie />);
    expect(document.cookie).toContain(`${SIGNUP_INTENT_COOKIE}=team`);
  });

  it("writes nothing when there is no team intent in the URL", () => {
    window.history.replaceState({}, "", "/sign-up");
    render(<SignupIntentCookie />);
    expect(document.cookie).not.toContain(SIGNUP_INTENT_COOKIE);
  });
});
