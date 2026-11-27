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
  beforeEach(() => window.sessionStorage.clear());
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
    expect(opts.capture_pageview).toBe("history_change"); // the SDK's own page views: on load and on each navigation
    expect(opts.capture_pageleave).toBe(false);
    expect(opts.autocapture).toBe(false);
    // Session replay masking defaults.
    const recording = opts.session_recording as {
      sampleRate: number;
      maskAllInputs: boolean;
      blockSelector: string;
      maskCapturedNetworkRequestFn: (request: { name: string; requestBody?: string }) => { name: string; requestBody?: string };
    };
    // Every session is recorded while signups are few (2026-09-30).
    expect(recording.sampleRate).toBe(1);
    expect(recording.maskAllInputs).toBe(true);
    expect(recording.blockSelector).toBe("[data-private]");
    expect(recording.maskCapturedNetworkRequestFn({ name: "https://thelightbulb.company/chat?path=/private", requestBody: "secret" }))
      .toMatchObject({ name: "https://thelightbulb.company/chat", requestBody: undefined });
    type Capture = { event?: string; properties: Record<string, unknown>; $set?: Record<string, unknown>; $set_once?: Record<string, unknown> };
    const beforeSend = opts.before_send as (event: Capture) => Capture | null;
    expect(beforeSend({ properties: { $current_url: "/sign-in?__clerk_ticket=t" } })?.properties.$current_url)
      .toBe("/sign-in");
    const person = beforeSend({
      event: "$identify",
      properties: {
        $session_entry_url: "/sign-in?__clerk_ticket=t",
        $set_once: { $initial_current_url: "/sign-in?__clerk_ticket=t" },
      },
      $set: { $current_url: "/sign-in?__clerk_ticket=t" },
      $set_once: { $initial_current_url: "/sign-in?__clerk_ticket=t", $initial_referrer: "/sign-in?__clerk_ticket=t" },
    });
    expect(person?.properties.$session_entry_url).toBe("/sign-in");
    expect(person?.properties.$set_once).toEqual({ $initial_current_url: "/sign-in" });
    expect(person?.$set).toEqual({ $current_url: "/sign-in" });
    expect(person?.$set_once).toEqual({ $initial_current_url: "/sign-in", $initial_referrer: "/sign-in" });
    const replay = beforeSend({ event: "$snapshot", properties: { $snapshot_data: [
      { type: 4, data: { href: "https://thelightbulb.company/sign-in?__clerk_ticket=t" } },
      { type: 5, data: { tag: "$url_changed", payload: { href: "/sign-in?__clerk_ticket=t" } } },
    ] } });
    expect(replay?.properties.$snapshot_data).toEqual([
      { type: 4, data: { href: "https://thelightbulb.company/sign-in" } },
      { type: 5, data: { tag: "$url_changed", payload: { href: "/sign-in" } } },
    ]);
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

  it("identifies the signed-in person with their email only", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    mockClerkState.isSignedIn = true;
    mockClerkState.userId = "user_123";
    mockClerkState.user = {
      primaryEmailAddress: { emailAddress: "user@example.com" },
      firstName: "A",
      lastName: "User",
    };

    const mod = await import("../PostHogProvider");
    render(<mod.PostHogProvider><div>child</div></mod.PostHogProvider>);

    expect(mockIdentify).toHaveBeenCalledTimes(1);
    expect(mockIdentify).toHaveBeenCalledWith("user_123", { email: "user@example.com" });
  });

  it("sends no $pageview and no landing event of its own", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
    const mod = await import("../PostHogProvider");
    render(<mod.PostHogProvider><div /></mod.PostHogProvider>);
    expect(mockCapture).not.toHaveBeenCalled();
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
      expect(resolveEnvironment("dev.flickmeon.ai")).toBe("launch");
    });

    it("is dev for dev hosts, the dev admin, localhost, and an unknown host", async () => {
      const { resolveEnvironment } = await import("../PostHogProvider");
      expect(resolveEnvironment("dev.thelightbulb.company")).toBe("dev");
      expect(resolveEnvironment("admin.dev.thelightbulb.company")).toBe("dev");
      expect(resolveEnvironment("localhost")).toBe("dev");
      expect(resolveEnvironment(undefined)).toBe("dev");
    });
  });

  it("registers environment=dev on a non-product host (jsdom localhost) and re-registers it after reset() on sign-out", async () => {
    process.env.NEXT_PUBLIC_POSTHOG_KEY = "phc_test_key_123";
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
