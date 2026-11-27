import { describe, it, expect, beforeEach, vi } from "vitest";

// Hoisted mock: the `capture` helper imports posthog-js; we need the mock
// installed before the module-under-test is imported.
const mockCapture = vi.fn();
const mockCaptureException = vi.fn();
const mockIdentify = vi.fn();
const mockReset = vi.fn();
const mockState: { __loaded: boolean } = { __loaded: true };

vi.mock("posthog-js", () => {
  const posthog = {
    get __loaded() {
      return mockState.__loaded;
    },
    capture: (...args: unknown[]) => mockCapture(...args),
    captureException: (...args: unknown[]) => mockCaptureException(...args),
    identify: (...args: unknown[]) => mockIdentify(...args),
    reset: (...args: unknown[]) => mockReset(...args),
  };
  return { default: posthog };
});

// Re-import the module under test per test so the `window` stub flips
// cleanly between SSR and browser. `vi.resetModules()` in beforeEach
// guarantees a fresh module evaluation.
async function importAnalytics() {
  const mod = await import("../analytics");
  return mod;
}

function installLocalStorageStub() {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
}

describe("analytics.capture", () => {
  beforeEach(() => {
    vi.resetModules();
    mockCapture.mockReset();
    mockCaptureException.mockReset();
    mockIdentify.mockReset();
    mockReset.mockReset();
    mockState.__loaded = true;
    installLocalStorageStub();
    window.localStorage.clear();
  });

  it("no-ops when window is undefined (SSR)", async () => {
    // Stash window and blow it away so the guard trips. Restore after.
    const originalWindow = globalThis.window;
    // @ts-expect-error — deliberately shadowing for SSR simulation
    delete globalThis.window;
    try {
      const { capture } = await importAnalytics();
      capture("spark_download_clicked", { source: "nav" });
      expect(mockCapture).not.toHaveBeenCalled();
    } finally {
      globalThis.window = originalWindow;
    }
  });

  it("no-ops when PostHog has not finished loading (__loaded=false)", async () => {
    mockState.__loaded = false;
    const { capture } = await importAnalytics();
    capture("spark_download_clicked", { source: "nav" });
    expect(mockCapture).not.toHaveBeenCalled();
  });

  it("forwards to posthog.capture when loaded", async () => {
    mockState.__loaded = true;
    const { capture } = await importAnalytics();
    capture("spark_download_clicked", { source: "hero" });
    expect(mockCapture).toHaveBeenCalledTimes(1);
    expect(mockCapture).toHaveBeenCalledWith("spark_download_clicked", { source: "hero" });
  });

  it("drops secrets from analytics URLs", async () => {
    const { withoutChatQuery } = await importAnalytics();
    expect(withoutChatQuery("/?utm_source=hn")).toBe("/?utm_source=hn");
    // /j is tracked, but its fragment (the ticket) is dropped; the query stays.
    expect(withoutChatQuery("https://x.co/j#ticket")).toBe("https://x.co/j");
    expect(withoutChatQuery("/j?utm_source=hn#ticket")).toBe("/j?utm_source=hn");
    expect(withoutChatQuery("https://x.co/sign-in?__clerk_ticket=t&__clerk_status=sign_up&utm_source=hn"))
      .toBe("https://x.co/sign-in?utm_source=hn");
    expect(withoutChatQuery("/sign-in?__clerk_ticket=t")).toBe("/sign-in");
    const card = encodeURIComponent("/chat?panel=teams-approval-detail&detailId=ap-secret");
    expect(withoutChatQuery(`https://x.co/sign-in?__clerk_ticket=t&redirect_url=${card}&utm_source=hn`))
      .toBe("https://x.co/sign-in?utm_source=hn");
    expect(withoutChatQuery(`/sign-in?redirect_url=${card}`)).toBe("/sign-in");
  });

  it("captureException forwards to posthog.captureException when loaded", async () => {
    mockState.__loaded = true;
    const { captureException } = await importAnalytics();
    const err = new Error("boom");
    captureException(err);
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
    expect(mockCaptureException).toHaveBeenCalledWith(err);
  });

  it("captureException no-ops when PostHog is not loaded", async () => {
    mockState.__loaded = false;
    const { captureException } = await importAnalytics();
    captureException(new Error("silenced"));
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it("identify forwards the person's email and nothing else is added", async () => {
    const { identify } = await importAnalytics();
    identify("user_123", { email: "user@example.com" });
    expect(mockIdentify).toHaveBeenCalledWith("user_123", { email: "user@example.com" });
  });
});
