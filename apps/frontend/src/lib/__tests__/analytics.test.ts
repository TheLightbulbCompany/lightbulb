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
      capture("agent_created", { agent_id: "a1" });
      expect(mockCapture).not.toHaveBeenCalled();
    } finally {
      globalThis.window = originalWindow;
    }
  });

  it("no-ops when PostHog has not finished loading (__loaded=false)", async () => {
    mockState.__loaded = false;
    const { capture } = await importAnalytics();
    capture("agent_deleted", { agent_id: "a1" });
    expect(mockCapture).not.toHaveBeenCalled();
  });

  it("forwards to posthog.capture when loaded", async () => {
    mockState.__loaded = true;
    const { capture } = await importAnalytics();
    capture("chat_message_sent", { agent_id: "a1", message_length: 42 });
    expect(mockCapture).toHaveBeenCalledTimes(1);
    expect(mockCapture).toHaveBeenCalledWith("chat_message_sent", {
      agent_id: "a1",
      message_length: 42,
    }, undefined);
  });

  it("captures a browser handoff with its workspace and kind", async () => {
    const { captureHandoffCompleted } = await importAnalytics();
    captureHandoffCompleted("org_workspace", "approval_answered");
    expect(mockCapture).toHaveBeenCalledWith("handoff_completed", {
      workspace_id: "org_workspace",
      kind: "approval_answered",
    }, undefined);
  });

  it("does not capture a handoff without an organization workspace id", async () => {
    const { captureHandoffCompleted } = await importAnalytics();
    captureHandoffCompleted(undefined, "task_moved");
    captureHandoffCompleted("user_123", "task_moved");
    expect(mockCapture).not.toHaveBeenCalled();
  });

  it("drops file-path query strings from chat analytics URLs", async () => {
    const { withoutChatQuery } = await importAnalytics();
    expect(withoutChatQuery("/chat?panel=file-viewer&path=%2FUsers%2Falice%2Fsecret")).toBe("/chat");
    expect(withoutChatQuery("/?utm_source=hn")).toBe("/?utm_source=hn");
    // Invitation credentials and names never reach analytics.
    expect(withoutChatQuery("https://x.co/join/invite?workspace=Acme&inviter=Sam&__clerk_ticket=t")).toBe("https://x.co/join/invite");
    expect(withoutChatQuery("/join/org_1#secret")).toBe("/join/org_1");
    // /j is tracked, but its fragment (the ticket) is dropped; the query stays.
    expect(withoutChatQuery("https://x.co/j#ticket")).toBe("https://x.co/j");
    expect(withoutChatQuery("/j?utm_source=hn#ticket")).toBe("/j?utm_source=hn");
    expect(withoutChatQuery("https://x.co/sign-up?__clerk_ticket=t&__clerk_status=sign_up&utm_source=hn"))
      .toBe("https://x.co/sign-up?utm_source=hn");
    expect(withoutChatQuery("/sign-in?__clerk_ticket=t")).toBe("/sign-in");
    // T09: the approval card an invitation opens after sign-up is a private id.
    const card = encodeURIComponent("/chat?panel=teams-approval-detail&detailId=ap-secret");
    expect(withoutChatQuery(`https://x.co/sign-up?__clerk_ticket=t&redirect_url=${card}&utm_source=hn`))
      .toBe("https://x.co/sign-up?utm_source=hn");
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

  it("persists attribution from the current URL and attaches it to events", async () => {
    window.history.replaceState({}, "", "/?utm_source=hn&utm_campaign=launch");
    const { capture, refreshAttribution } = await importAnalytics();

    refreshAttribution();
    capture("signup_cta_clicked", { cta_id: "hero_funnel_submit" });

    expect(mockCapture).toHaveBeenCalledWith("signup_cta_clicked", {
      first_touch_timestamp: expect.any(String),
      first_touch_landing_path: "/?utm_source=hn&utm_campaign=launch",
      first_touch_utm_source: "hn",
      first_touch_utm_campaign: "launch",
      last_touch_timestamp: expect.any(String),
      last_touch_landing_path: "/?utm_source=hn&utm_campaign=launch",
      last_touch_utm_source: "hn",
      last_touch_utm_campaign: "launch",
      cta_id: "hero_funnel_submit",
    }, undefined);
  });

  it("attaches stored attribution on identify", async () => {
    window.history.replaceState({}, "", "/?utm_source=directory&utm_medium=referral");
    const { identify, refreshAttribution } = await importAnalytics();

    refreshAttribution();
    identify("user_123", { email: "user@example.com" });

    expect(mockIdentify).toHaveBeenCalledWith("user_123", {
      first_touch_timestamp: expect.any(String),
      first_touch_landing_path: "/?utm_source=directory&utm_medium=referral",
      first_touch_utm_source: "directory",
      first_touch_utm_medium: "referral",
      last_touch_timestamp: expect.any(String),
      last_touch_landing_path: "/?utm_source=directory&utm_medium=referral",
      last_touch_utm_source: "directory",
      last_touch_utm_medium: "referral",
      email: "user@example.com",
    });
  });
});
