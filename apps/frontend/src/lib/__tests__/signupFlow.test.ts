import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockCapture = vi.fn();
vi.mock("@/lib/analytics", () => ({ capture: (...args: unknown[]) => mockCapture(...args) }));

import {
  browserContext,
  captureSignupCta,
  currentSignupStep,
  rotateSignupFlowId,
  setSignupStep,
  signupFlowId,
} from "@/lib/signupFlow";

const FLOW_ID_KEY = "lightbulb:signup_flow_id:v1";

describe("signupFlowId", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("mints once and returns the same id on every later call", () => {
    const first = signupFlowId();
    expect(first).toBeTruthy();
    expect(signupFlowId()).toBe(first);
    expect(window.localStorage.getItem(FLOW_ID_KEY)).toBe(first);
  });

  it("rotates to a NEW id on demand, so a second signup on one browser gets its own journey", () => {
    const first = signupFlowId();
    rotateSignupFlowId();
    const second = signupFlowId();
    expect(second).toBeTruthy();
    expect(second).not.toBe(first);
    expect(window.localStorage.getItem(FLOW_ID_KEY)).toBe(second);
  });

  it("rotates even when storage throws (memory fallback is cleared too)", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    const first = signupFlowId();
    rotateSignupFlowId();
    expect(signupFlowId()).not.toBe(first);
  });

  it("still returns a stable id when storage throws", () => {
    // Safari private mode and several in-app webviews — the exact cohort the
    // flow id exists to measure. A throw here must not leave the journey
    // unjoinable for the whole page session.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    const first = signupFlowId();
    expect(first).toBeTruthy();
    expect(signupFlowId()).toBe(first);
  });
});

describe("setSignupStep", () => {
  it("publishes the current step and clears it", () => {
    setSignupStep("provider");
    expect(currentSignupStep()).toBe("provider");
    setSignupStep(null);
    expect(currentSignupStep()).toBeNull();
  });
});

describe("browserContext", () => {
  // Real UAs. The iOS in-app webviews all claim "Mobile/…Safari", which is
  // why PostHog's own $browser reports every one of them as Mobile Safari.
  const UAS: Array<[string, string, boolean]> = [
    [
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 334.0.3.28.99 (iPhone14,3; iOS 17_5; en_US)",
      "instagram",
      true,
    ],
    [
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBAV/468.0.0.47.107;FBBV/1234]",
      "facebook",
      true,
    ],
    [
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
      "safari",
      false,
    ],
    [
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/126.0.6478.54 Mobile/15E148 Safari/604.1",
      "chrome",
      false,
    ],
    [
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
      "chrome",
      false,
    ],
    ["Mozilla/5.0 (compatible; SomeBot/1.0)", "other", false],
  ];

  it.each(UAS)("classifies %s", (ua, expected, inApp) => {
    expect(browserContext(ua)).toEqual({
      browser_context: expected,
      is_in_app_browser: inApp,
    });
  });

  it("does not confuse an in-app webview with plain mobile Safari", () => {
    const instagram = UAS[0][0];
    const safari = UAS[2][0];
    expect(browserContext(instagram).browser_context).not.toBe(
      browserContext(safari).browser_context,
    );
  });
});

describe("captureSignupCta", () => {
  beforeEach(() => mockCapture.mockClear());

  it("carries the five required fields, the optional provider, and any extra properties", () => {
    captureSignupCta({
      ctaId: "hero_funnel_submit",
      label: "Start",
      surface: "landing_hero_funnel",
      providerChoice: "chatgpt_oauth",
      properties: { path: "grow" },
    });
    expect(mockCapture).toHaveBeenCalledWith("signup_cta_clicked", {
      cta_id: "hero_funnel_submit",
      cta_label: "Start",
      surface: "landing_hero_funnel",
      destination: "/sign-up",
      route: window.location.pathname,
      provider_choice: "chatgpt_oauth",
      path: "grow",
    });
  });
});
