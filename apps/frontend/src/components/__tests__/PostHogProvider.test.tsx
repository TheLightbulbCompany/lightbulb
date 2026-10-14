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
const mockStopRecording = vi.fn();
const mockStartRecording = vi.fn();
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
    stopSessionRecording: mockStopRecording,
    startSessionRecording: mockStartRecording,
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
const mockParamsCache = vi.hoisted(() => new Map<string, URLSearchParams>());
vi.mock("next/navigation", () => ({
  usePathname: () => mockRouterState.pathname,
  // Stable per search string, like the real hook between navigations —
  // otherwise every render hands the effect a new object and the double-
  // fire regression test would pass for the wrong reason.
  useSearchParams: () => {
    let p = mockParamsCache.get(mockRouterState.search);
    if (!p) {
      p = new URLSearchParams(mockRouterState.search);
      mockParamsCache.set(mockRouterState.search, p);
    }
    return p;
  },
}));

describe("PostHogProvider", () => {
  const originalKey = process.env.NEXT_PUBLIC_POSTHOG_KEY;
  const originalApiUrl = process.env.NEXT_PUBLIC_API_URL;

  afterEach(() => {
    if (originalKey === undefined) {
      delete process.env.NEXT_PUBLIC_POSTHOG_KEY;
    } else {
      process.env.NEXT_PUBLIC_POSTHOG_KEY = originalKey;
    }
    if (originalApiUrl === undefined) {
      delete process.env.NEXT_PUBLIC_API_URL;
    } else {
      process.env.NEXT_PUBLIC_API_URL = originalApiUrl;
    }
  });

  beforeEach(() => {
    vi.resetModules();
    mockInit.mockClear();
    mockCapture.mockClear();
    mockCaptureException.mockClear();
    mockIdentify.mockClear();
    mockReset.mockClear();
    mockRegister.mockClear();
    mockStopRecording.mockClear();
    mockStartRecording.mockClear();
    document.body.classList.remove("ph-no-capture");
    window.history.replaceState({}, "", "/");
    mockRouterState.pathname = "/";
    mockRouterState.search = "";
    mockClerkState.isSignedIn = false;
    mockClerkState.userId = null;
    mockClerkState.user = null;
    window.localStorage.clear();
    delete process.env.NEXT_PUBLIC_POSTHOG_KEY;
    delete process.env.NEXT_PUBLIC_API_URL;
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
      sampleRate: number;
      maskAllInputs: boolean;
      blockSelector: string;
      maskCapturedNetworkRequestFn: (request: { name: string; requestBody?: string }) => { name: string; requestBody?: string };
    };
    // Every pre-chat session is recorded while signups are few (2026-09-30); a
    // sampled replay missed the one new-user session that needed watching.
    // /chat stays unrecorded regardless of the rate (route effect below).
    expect(recording.sampleRate).toBe(1);
    expect(recording.maskAllInputs).toBe(true);
    expect(recording.blockSelector).toBe("[data-private]");
    expect(recording.maskCapturedNetworkRequestFn({ name: "https://thelightbulb.company/chat?path=/private", requestBody: "secret" }))
      .toMatchObject({ name: "https://thelightbulb.company/chat", requestBody: undefined });
    type Capture = { event?: string; properties: Record<string, unknown>; $set?: Record<string, unknown>; $set_once?: Record<string, unknown> };
    const beforeSend = opts.before_send as (event: Capture) => Capture | null;
    expect(beforeSend({ properties: { $current_url: "/chat?path=/private" } })?.properties.$current_url)
      .toBe("/chat");
    const person = beforeSend({
      event: "$identify",
      properties: {
        $session_entry_url: "/chat?path=/private",
        $set_once: { $initial_current_url: "/chat?path=/private" },
      },
      $set: { $current_url: "/chat?path=/private" },
      $set_once: { $initial_current_url: "/chat?path=/private", $initial_referrer: "/chat?path=/private" },
    });
    expect(person?.properties.$session_entry_url).toBe("/chat");
    expect(person?.properties.$set_once).toEqual({ $initial_current_url: "/chat" });
    expect(person?.$set).toEqual({ $current_url: "/chat" });
    expect(person?.$set_once).toEqual({ $initial_current_url: "/chat", $initial_referrer: "/chat" });
    const replay = beforeSend({ event: "$snapshot", properties: { $snapshot_data: [
      { type: 4, data: { href: "https://thelightbulb.company/chat?path=%2Fprivate" } },
      { type: 5, data: { tag: "$url_changed", payload: { href: "/chat?path=%2Fprivate" } } },
    ] } });
    expect(replay?.properties.$snapshot_data).toEqual([
      { type: 4, data: { href: "https://thelightbulb.company/chat" } },
      { type: 5, data: { tag: "$url_changed", payload: { href: "/chat" } } },
    ]);
  });

  it("blocks body portals and stops replay on the chat route", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    mockRouterState.pathname = "/chat";
    window.history.replaceState({}, "", "/chat?path=%2Fprivate%2Ffolder");
    const mod = await import("../PostHogProvider");
    render(<mod.PostHogProvider><div>private chat</div></mod.PostHogProvider>);

    expect((mockInit.mock.calls[0][1] as Record<string, unknown>).disable_session_recording).toBe(true);
    expect(document.body.classList.contains("ph-no-capture")).toBe(true);
    expect(mockStopRecording).toHaveBeenCalled();
    const beforeSend = (mockInit.mock.calls[0][1] as Record<string, unknown>).before_send as (
      event: { event: string; properties: Record<string, unknown> },
    ) => unknown;
    expect(beforeSend({ event: "$snapshot", properties: { $snapshot_data: [{ type: 4, data: { href: "/chat?path=/private" } }] } }))
      .toBeNull();
  });

  it("treats a session link page like /chat and strips its token from every URL", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    mockRouterState.pathname = "/s";
    window.history.replaceState({}, "", "/s?port=53123#secret-token");
    const mod = await import("../PostHogProvider");
    render(<mod.PostHogProvider><div>transcript</div></mod.PostHogProvider>);

    const options = mockInit.mock.calls[0][1] as Record<string, unknown>;
    expect(options.disable_session_recording).toBe(true);
    expect(document.body.classList.contains("ph-no-capture")).toBe(true);
    expect(mockStopRecording).toHaveBeenCalled();
    const ignore = (options.autocapture as { url_ignorelist: RegExp[] }).url_ignorelist;
    expect(ignore.some(r => r.test("https://thelightbulb.company/s#secret-token"))).toBe(true);
    expect(ignore.some(r => r.test("https://thelightbulb.company/settings"))).toBe(false);
    const beforeSend = options.before_send as (
      event: { event: string; properties: Record<string, unknown>; $set_once?: Record<string, unknown> },
    ) => { properties: Record<string, unknown>; $set_once?: Record<string, unknown> } | null;
    expect(beforeSend({ event: "$snapshot", properties: { $snapshot_data: [] } })).toBeNull();
    const sent = beforeSend({
      event: "$identify",
      properties: { $current_url: "https://thelightbulb.company/s#secret-token" },
      $set_once: { $initial_current_url: "https://thelightbulb.company/s?port=53123#secret-token" },
    });
    expect(JSON.stringify(sent)).not.toContain("secret-token");
    expect(JSON.stringify(sent)).not.toContain("53123");
  });

  it("stops replay when navigation enters chat from onboarding", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    mockRouterState.pathname = "/onboarding";
    window.history.replaceState({}, "", "/onboarding");
    const mod = await import("../PostHogProvider");
    const { rerender } = render(<mod.PostHogProvider><div>child</div></mod.PostHogProvider>);
    expect(mockStartRecording).toHaveBeenCalled();

    mockRouterState.pathname = "/chat";
    window.history.replaceState({}, "", "/chat?path=%2Fprivate%2Ffolder");
    rerender(<mod.PostHogProvider><div>child</div></mod.PostHogProvider>);
    expect(document.body.classList.contains("ph-no-capture")).toBe(true);
    expect(mockStopRecording).toHaveBeenCalled();
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

  it("sends $pageview and landing_viewed ONCE per landing load even when Clerk resolves after mount", async () => {
    // Clerk's useAuth() reports isSignedIn=undefined on first render and a
    // boolean a tick later. The pageview effect depends on it (for the
    // sign-up-intent stash), so without a URL guard the same landing load
    // produced two of everything — the 2× a live prod signup measured.
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    mockRouterState.pathname = "/";
    mockRouterState.search = "";
    mockClerkState.isSignedIn = undefined as unknown as boolean;
    mockClerkState.userId = null;

    const mod = await import("../PostHogProvider");
    const { rerender } = render(
      <mod.PostHogProvider>
        <div>child</div>
      </mod.PostHogProvider>,
    );
    mockClerkState.isSignedIn = false; // Clerk resolves: signed out
    // A NEW element, deliberately: re-rendering the same element reference
    // lets React bail out before PostHogPageview ever re-renders, and the
    // effect under test would never get its second run.
    rerender(
      <mod.PostHogProvider>
        <div>child</div>
      </mod.PostHogProvider>,
    );

    const names = mockCapture.mock.calls.map((c) => c[0]);
    expect(names.filter((n) => n === "$pageview")).toHaveLength(1);
    expect(names.filter((n) => n === "landing_viewed")).toHaveLength(1);
  });

  it("does NOT stash a sign-up intent while Clerk is unresolved or for a signed-in user", async () => {
    // `isSignedIn` is `undefined` on first render. The old `!isSignedIn`
    // treated that as signed-out and stashed an intent for anyone opening
    // /sign-up — including existing accounts, which PostHogIdentify then
    // replayed as a spurious signup_started{clerk_signup_completed}.
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    mockRouterState.pathname = "/sign-up";
    mockRouterState.search = "";
    window.localStorage.removeItem("isol8:signup_intent:v1");

    mockClerkState.isSignedIn = undefined as unknown as boolean;
    mockClerkState.userId = null;
    const mod = await import("../PostHogProvider");
    const { rerender, unmount } = render(<mod.PostHogProvider><div /></mod.PostHogProvider>);
    expect(window.localStorage.getItem("isol8:signup_intent:v1")).toBeNull();

    mockClerkState.isSignedIn = true; // resolves: an existing account on /sign-up
    mockClerkState.userId = "user_existing";
    rerender(<mod.PostHogProvider><div /></mod.PostHogProvider>);
    expect(window.localStorage.getItem("isol8:signup_intent:v1")).toBeNull();
    unmount();

    mockClerkState.isSignedIn = false; // resolves: genuinely signed out
    mockClerkState.userId = null;
    render(<mod.PostHogProvider><div /></mod.PostHogProvider>);
    expect(window.localStorage.getItem("isol8:signup_intent:v1")).not.toBeNull();
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

  describe("resolveEnvironment", () => {
    it("is prod only on the product hosts", async () => {
      const { resolveEnvironment } = await import("../PostHogProvider");
      expect(resolveEnvironment("thelightbulb.company")).toBe("prod");
      expect(resolveEnvironment("www.thelightbulb.company")).toBe("prod");
      expect(resolveEnvironment("admin.thelightbulb.company")).toBe("prod");
    });

    it("is launch on flickmeon.ai", async () => {
      const { resolveEnvironment } = await import("../PostHogProvider");
      expect(resolveEnvironment("flickmeon.ai")).toBe("launch");
    });

    it("is dev for dev hosts, the dev admin, localhost, and an unknown host", async () => {
      const { resolveEnvironment } = await import("../PostHogProvider");
      expect(resolveEnvironment("dev.thelightbulb.company")).toBe("dev");
      expect(resolveEnvironment("admin.dev.thelightbulb.company")).toBe("dev");
      expect(resolveEnvironment("localhost")).toBe("dev");
      expect(resolveEnvironment(undefined)).toBe("dev");
    });
  });

  it("registers environment=dev on a non-product host (jsdom localhost), even with the prod API URL, and re-registers it after reset() on sign-out", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    process.env.NEXT_PUBLIC_API_URL = "https://api.thelightbulb.company/api/v1";
    mockClerkState.isSignedIn = true;
    mockClerkState.userId = "user_123";

    const mod = await import("../PostHogProvider");
    const { rerender } = render(<mod.PostHogProvider><div>child</div></mod.PostHogProvider>);
    expect(mockRegister).toHaveBeenCalledWith(expect.objectContaining({ environment: "dev" }));
    mockRegister.mockClear();

    // reset() clears super properties on sign-out — environment must be re-registered.
    mockClerkState.isSignedIn = false;
    mockClerkState.userId = null;
    rerender(<mod.PostHogProvider><div>child</div></mod.PostHogProvider>);
    expect(mockReset).toHaveBeenCalledTimes(1);
    expect(mockRegister).toHaveBeenCalledWith(expect.objectContaining({ environment: "dev" }));
  });
});
