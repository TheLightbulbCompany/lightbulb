import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

// Mock posthog-js before any imports that use it
const mockInit = vi.fn();
const mockCapture = vi.fn();
const mockCaptureException = vi.fn();
const mockIdentify = vi.fn();
const mockReset = vi.fn();
const mockRegister = vi.fn();
const mockRouterState = vi.hoisted(() => ({
  pathname: "/",
  search: "",
}));
const mockClerkState = vi.hoisted(() => ({
  isSignedIn: false,
  userId: null as string | null,
  user: null as null | {
    primaryEmailAddress?: { emailAddress?: string };
    firstName?: string | null;
    lastName?: string | null;
  },
}));

vi.mock("posthog-js", () => {
  const posthog = {
    __loaded: true,
    init: mockInit,
    capture: mockCapture,
    captureException: mockCaptureException,
    identify: mockIdentify,
    reset: mockReset,
    register: mockRegister,
  };
  return { default: posthog };
});

vi.mock("posthog-js/react", () => ({
  PostHogProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="ph-provider">{children}</div>
  ),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isSignedIn: mockClerkState.isSignedIn, userId: mockClerkState.userId }),
  useUser: () => ({ user: mockClerkState.user }),
}));

// Stubs for usePathname/useSearchParams: PostHogPageview reads them and
// they'd otherwise throw outside a Next router context.
vi.mock("next/navigation", () => ({
  usePathname: () => mockRouterState.pathname,
  useSearchParams: () => new URLSearchParams(mockRouterState.search),
}));

describe("PostHogProvider", () => {
  const originalKey = process.env.NEXT_PUBLIC_POSTHOG_KEY;

  afterEach(() => {
    if (originalKey === undefined) {
      delete process.env.NEXT_PUBLIC_POSTHOG_KEY;
    } else {
      process.env.NEXT_PUBLIC_POSTHOG_KEY = originalKey;
    }
  });

  beforeEach(() => {
    vi.resetModules();
    mockInit.mockClear();
    mockCapture.mockClear();
    mockCaptureException.mockClear();
    mockIdentify.mockClear();
    mockReset.mockClear();
    mockRouterState.pathname = "/";
    mockRouterState.search = "";
    mockClerkState.isSignedIn = false;
    mockClerkState.userId = null;
    mockClerkState.user = null;
    window.localStorage.clear();
    delete process.env.NEXT_PUBLIC_POSTHOG_KEY;
  });

  it("does NOT initialize PostHog when NEXT_PUBLIC_POSTHOG_KEY is not set", async () => {
    delete process.env.NEXT_PUBLIC_POSTHOG_KEY;

    const mod = await import("../PostHogProvider");
    render(
      <mod.PostHogProvider>
        <div>child content</div>
      </mod.PostHogProvider>
    );

    expect(mockInit).not.toHaveBeenCalled();
  });

  it("initializes PostHog with session_recording + same-origin api_host when key is set", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";

    const mod = await import("../PostHogProvider");
    render(
      <mod.PostHogProvider>
        <div>child content</div>
      </mod.PostHogProvider>
    );

    expect(mockInit).toHaveBeenCalledTimes(1);
    const [key, opts] = mockInit.mock.calls[0] as [string, Record<string, unknown>];
    expect(key).toBe("phc_test_key_123");
    // Same-origin proxy (see the next.config.ts /ingest rewrite) — not
    // the env-var driven host from the old test.
    expect(opts.api_host).toBe("/ingest");
    expect(opts.ui_host).toBe("https://us.posthog.com");
    expect(opts.person_profiles).toBe("identified_only");
    expect(opts.capture_pageview).toBe(false);
    expect(opts.capture_pageleave).toBe(true);
    // Session replay must be enabled with masking defaults.
    expect(opts.disable_session_recording).toBe(false);
    const recording = opts.session_recording as {
      maskAllInputs: boolean;
      maskInputOptions: { password: boolean; email: boolean };
      blockSelector: string;
    };
    expect(recording.maskAllInputs).toBe(false);
    expect(recording.maskInputOptions.password).toBe(true);
    expect(recording.blockSelector).toBe("[data-private]");
  });

  it("renders children when NEXT_PUBLIC_POSTHOG_KEY is not set", async () => {
    delete process.env.NEXT_PUBLIC_POSTHOG_KEY;

    const mod = await import("../PostHogProvider");
    render(
      <mod.PostHogProvider>
        <div data-testid="child">Hello</div>
      </mod.PostHogProvider>
    );

    expect(screen.getByTestId("child")).toBeTruthy();
    expect(screen.getByText("Hello")).toBeTruthy();
  });

  it("renders children when NEXT_PUBLIC_POSTHOG_KEY is set", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";

    const mod = await import("../PostHogProvider");
    render(
      <mod.PostHogProvider>
        <div data-testid="child">Hello</div>
      </mod.PostHogProvider>
    );

    expect(screen.getByTestId("child")).toBeTruthy();
    expect(screen.getByText("Hello")).toBeTruthy();
  });

  it("captures signup_started after Clerk identifies the user from a sign-up intent", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    mockRouterState.pathname = "/chat";
    mockRouterState.search = "";
    window.localStorage.setItem("isol8:signup_intent:v1", JSON.stringify({
      path: "/sign-up?provider=byo_key",
      provider_choice: "byo_key",
      timestamp: "2026-08-20T05:58:00.000Z",
    }));
    mockClerkState.isSignedIn = true;
    mockClerkState.userId = "user_123";
    mockClerkState.user = {
      primaryEmailAddress: { emailAddress: "user@example.com" },
      firstName: "A",
      lastName: "User",
    };

    const mod = await import("../PostHogProvider");
    render(
      <mod.PostHogProvider>
        <div>child</div>
      </mod.PostHogProvider>
    );

    expect(mockIdentify).toHaveBeenCalledWith("user_123", expect.objectContaining({
      email: "user@example.com",
    }));
    // Trailing `undefined` is capture()'s CaptureOptions passthrough — this
    // call site sends none.
    expect(mockCapture).toHaveBeenCalledWith("signup_started", expect.objectContaining({
      source: "clerk_signup_completed",
      identity_stitched: true,
      provider_choice: "byo_key",
      signup_path: "/sign-up?provider=byo_key",
    }), undefined);
  });

  it("does not recapture stitched signup_started for the same user", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    mockRouterState.pathname = "/chat";
    mockRouterState.search = "";
    mockClerkState.isSignedIn = true;
    mockClerkState.userId = "user_123";
    window.localStorage.setItem("isol8:signup_intent:v1", JSON.stringify({ path: "/sign-up" }));
    window.localStorage.setItem("isol8:signup_started_identified:user_123", "1");

    const mod = await import("../PostHogProvider");
    render(
      <mod.PostHogProvider>
        <div>child</div>
      </mod.PostHogProvider>
    );

    // capture() always forwards a third (options) argument, so a two-argument
    // matcher here could never match and the assertion would be vacuous.
    expect(mockCapture).not.toHaveBeenCalledWith("signup_started", expect.anything(), undefined);
    expect(mockCapture.mock.calls.map((c) => c[0])).not.toContain("signup_started");
  });

  it("forwards uncaught window errors to posthog.captureException", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";

    const mod = await import("../PostHogProvider");
    render(
      <mod.PostHogProvider>
        <div>child</div>
      </mod.PostHogProvider>
    );

    const err = new Error("kaboom");
    const ev = new ErrorEvent("error", { error: err, message: "kaboom" });
    window.dispatchEvent(ev);

    expect(mockCaptureException).toHaveBeenCalledTimes(1);
    expect(mockCaptureException).toHaveBeenCalledWith(err);
  });
});
