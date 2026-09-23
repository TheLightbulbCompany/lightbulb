// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import React from "react";

import { useOnboardingStatus } from "./useOnboardingStatus";
import type { StatusContract } from "./statusContract";

/**
 * Isolated per-test SWR cache (the pattern the web frontend's
 * useProvisioningState.test.tsx already uses) so tests never leak state
 * through the module-level SWR cache.
 */
function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(SWRConfig, { value: { provider: () => new Map() } }, children);
}

function contractBody(overrides: Partial<StatusContract> = {}): StatusContract {
  return {
    lifecycle: "provisioning",
    runtime_phase: "provisioning",
    chat_available: false,
    container_status: "provisioning",
    container_substatus: "service_created",
    parked: false,
    onboarded: false,
    last_error: null,
    last_error_at: null,
    service_name: "openclaw-user_x-abcd",
    created_at: null,
    updated_at: null,
    region: "us-east-1",
    generations: { provision_generation: null, onboarding_generation: null },
    funnel: { state: "not_started", goal_id: null, project_id: null, submitted_at: null, completed_at: null },
    bootstrap: { state: "not_started", dispatch_id: null, dispatched_at: null, delivered_at: null, attempts: 0, last_error: null },
    provider: { choice: null, byo_provider: null, is_org: false },
    teams_state: null,
    ...overrides,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A `Response` body can only be read once — a mock that polls multiple
 * times must build a FRESH Response per call, not reuse one instance
 * (`mockResolvedValue` reuses the same value/instance on every call). */
function jsonResponder(status: number, body: unknown): () => Promise<Response> {
  return () => Promise.resolve(jsonResponse(status, body));
}

function makeApi() {
  return { apiUrl: "https://api.test", getToken: vi.fn().mockResolvedValue("tok") };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("useOnboardingStatus", () => {
  it("never polls: consumers sharing the key fetch once on mount and never again on a timer", async () => {
    // `/container/status` is a cold read — refetched on events, focus and reconnect, never on
    // an interval. `/chat` mounts this hook several times against one SWR key.
    vi.useFakeTimers();
    try {
      const fetchSpy = vi.fn(jsonResponder(200, contractBody()));
      global.fetch = fetchSpy as unknown as typeof fetch;
      const cache = new Map();
      const shared = ({ children }: { children: React.ReactNode }) =>
        React.createElement(SWRConfig, { value: { provider: () => cache } }, children);
      const mount = () =>
        renderHook(() => useOnboardingStatus({ api: makeApi(), enabled: true }), { wrapper: shared });

      mount(); mount(); mount(); mount();
      await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalled());
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      const afterMount = fetchSpy.mock.calls.length;
      expect(afterMount).toBe(1);

      // A not-ready status used to re-poll every 5-15s; now nothing fires on its own.
      await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60_000); });
      expect(fetchSpy.mock.calls.length).toBe(afterMount);
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears sticky state immediately when the workspace scope changes", async () => {
    global.fetch = vi.fn().mockImplementationOnce(jsonResponder(200, contractBody({ service_name: "org-a" })))
      .mockRejectedValue(new Error("org-b unavailable"));
    const api = makeApi();
    const { result, rerender } = renderHook(
      ({ scope }) => useOnboardingStatus({ api, enabled: true, scope }),
      { wrapper, initialProps: { scope: "org-a" } },
    );
    await waitFor(() => expect(result.current.classified?.kind).toBe("container"));
    rerender({ scope: "org-b" });
    expect(result.current.classified).toBeNull();
    await waitFor(() => expect(result.current.networkError).toBeDefined());
    expect(result.current.classified).toBeNull();
  });
  it("classifies a 200 container body and derives its UI phase", async () => {
    global.fetch = vi.fn().mockImplementation(jsonResponder(200, contractBody()));
    const { result } = renderHook(
      () => useOnboardingStatus({ api: makeApi(), enabled: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.classified?.kind).toBe("container"));
    expect(result.current.derived?.phase).toBe("cold_start");
    expect(result.current.networkError).toBeUndefined();
  });

  it("is rejection-safe: a raw network failure never throws out of the hook", async () => {
    global.fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const { result } = renderHook(
      () => useOnboardingStatus({ api: makeApi(), enabled: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.networkError).toBeDefined());
    expect(result.current.networkError?.message).toContain("Failed to fetch");
    expect(result.current.classified).toBeNull();
  });

  it("keeps the last good status when a single refetch fails", async () => {
    vi.useFakeTimers();
    try {
      const fetchSpy = vi
        .fn()
        .mockImplementation(() => Promise.resolve(jsonResponse(200, contractBody())));
      global.fetch = fetchSpy as unknown as typeof fetch;

      const { result } = renderHook(
        () => useOnboardingStatus({ api: makeApi(), enabled: true }),
        { wrapper },
      );

      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      expect(result.current.classified?.kind).toBe("container");

      // The next fetch fails outright — a transient blip, or the
      // AbortController firing on a slow request.
      fetchSpy.mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")));
      await act(async () => {
        result.current.refresh();
        await vi.advanceTimersByTimeAsync(100);
      });

      // The failure is REPORTED, but the last good status must SURVIVE it.
      // The rejection-safe fetcher resolves failures as data, so without
      // stickiness SWR overwrites the good response and `classified` blanks
      // until the next fetch — which unmounts every consumer rendering on
      // top of it (the first-run funnel most visibly, destroying whatever
      // the user had typed).
      expect(result.current.networkError).toBeDefined();
      expect(result.current.classified?.kind).toBe("container");
      expect(result.current.derived?.phase).toBe("cold_start");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the last good status when a refetch returns a server error", async () => {
    vi.useFakeTimers();
    try {
      const fetchSpy = vi
        .fn()
        .mockImplementation(() => Promise.resolve(jsonResponse(200, contractBody())));
      global.fetch = fetchSpy as unknown as typeof fetch;

      const { result } = renderHook(
        () => useOnboardingStatus({ api: makeApi(), enabled: true }),
        { wrapper },
      );

      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      expect(result.current.classified?.kind).toBe("container");

      // A 5xx takes a DIFFERENT path than a rejected fetch: the client
      // throws ApiError, the fetcher catches it, and it classifies as
      // `unrecognized` — a "classified" outcome. It is still a failure, not
      // a server state, so it must not become the new last-good either.
      fetchSpy.mockImplementation(() =>
        Promise.resolve(jsonResponse(503, { detail: "upstream unavailable" })),
      );
      await act(async () => {
        result.current.refresh();
        await vi.advanceTimersByTimeAsync(100);
      });

      expect(result.current.classified?.kind).toBe("container");
      expect(result.current.networkError).toBeDefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports an error when the FIRST fetch is a server error and there is no last good", async () => {
    global.fetch = vi
      .fn()
      .mockImplementation(() => Promise.resolve(jsonResponse(500, { detail: "boom" })));
    const { result } = renderHook(
      () => useOnboardingStatus({ api: makeApi(), enabled: true }),
      { wrapper },
    );
    // Nothing to fall back on, so this must surface as a real error rather
    // than a silently stuck UI (the contract useProvisioningState relies on).
    await waitFor(() => expect(result.current.networkError).toBeDefined());
    expect(result.current.classified).toBeNull();
  });

  it("does not fetch while disabled", async () => {
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy;
    renderHook(() => useOnboardingStatus({ api: makeApi(), enabled: false }), { wrapper });
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("appends the signup flow id as ?flow= WITHOUT letting it reach the SWR key", async () => {
    vi.useFakeTimers();
    try {
      const fetchSpy = vi.fn().mockImplementation(
        jsonResponder(200, contractBody({ lifecycle: "ready", runtime_phase: "ready", chat_available: true })),
      );
      global.fetch = fetchSpy;
      // A provider map we can read back, so the cache-key assertion below is
      // real rather than assumed.
      const cache = new Map();
      const keyedWrapper = ({ children }: { children: React.ReactNode }) =>
        React.createElement(SWRConfig, { value: { provider: () => cache } }, children);
      renderHook(
        () =>
          useOnboardingStatus({
            api: makeApi(),
            enabled: true,
            wake: () => true,
            flow: () => "flow-abc",
          }),
        { wrapper: keyedWrapper },
      );
      await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalled());
      const url = fetchSpy.mock.calls.at(-1)?.[0] as string;
      // Both params ride the REQUEST, with exactly one "?" between them.
      expect(url).toContain("wake=1");
      expect(url).toContain("flow=flow-abc");
      expect(url.match(/\?/g)?.length).toBe(1);
      // Without an explicit scope (the mobile/default behavior), the SWR
      // cache key stays the bare path; request params never become keys.
      const keys = [...cache.keys()].map(String);
      expect(keys).toContain("/container/status");
      expect(keys.some((k) => k.includes("flow="))).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("a hung fetcher times out via AbortController and settles as a network error; a refetch recovers", async () => {
    vi.useFakeTimers();
    try {
      // A genuinely hung request that respects AbortSignal, exactly like real fetch().
      let hungRejected = false;
      const hungFetch = (_url: unknown, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            hungRejected = true;
            reject(new DOMException("Aborted", "AbortError"));
          });
        });

      const fetchSpy = vi.fn(hungFetch);
      global.fetch = fetchSpy as unknown as typeof fetch;

      const { result } = renderHook(
        () => useOnboardingStatus({ api: makeApi(), enabled: true }),
        { wrapper },
      );

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_500);
      });
      expect(hungRejected).toBe(true);
      expect(result.current.networkError).toBeDefined();
      expect(result.current.classified).toBeNull();

      // The network recovers; the next event/focus/refresh refetch lands it.
      fetchSpy.mockImplementation(() => Promise.resolve(jsonResponse(200, contractBody())));
      await act(async () => {
        result.current.refresh();
        await vi.advanceTimersByTimeAsync(100);
      });
      expect(result.current.classified?.kind).toBe("container");
      expect(result.current.networkError).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("refresh() forces an immediate revalidation", async () => {
    const fetchSpy = vi.fn().mockImplementation(jsonResponder(200, contractBody()));
    global.fetch = fetchSpy;
    const { result } = renderHook(
      () => useOnboardingStatus({ api: makeApi(), enabled: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.classified).not.toBeNull());
    const before = fetchSpy.mock.calls.length;
    result.current.refresh();
    await waitFor(() => expect(fetchSpy.mock.calls.length).toBeGreaterThan(before));
  });

  it("refetches on the transition to visible (the dropped-event safety net)", async () => {
    vi.useFakeTimers();
    try {
      const fetchSpy = vi.fn().mockImplementation(jsonResponder(200, contractBody()));
      global.fetch = fetchSpy;
      let visible = false;
      const onChangeRef: { current: (() => void) | null } = { current: null };
      const visibility = {
        isVisible: () => visible,
        subscribe: (cb: () => void) => {
          onChangeRef.current = cb;
          return () => {
            onChangeRef.current = null;
          };
        },
      };
      renderHook(
        () => useOnboardingStatus({ api: makeApi(), enabled: true, visibility }),
        { wrapper },
      );
      await vi.waitFor(() => expect(fetchSpy).toHaveBeenCalled());
      const callsWhileHidden = fetchSpy.mock.calls.length;

      // The flip to visible must fire an immediate refetch.
      visible = true;
      onChangeRef.current?.();
      await vi.waitFor(() => expect(fetchSpy.mock.calls.length).toBeGreaterThan(callsWhileHidden));
    } finally {
      vi.useRealTimers();
    }
  });
});
