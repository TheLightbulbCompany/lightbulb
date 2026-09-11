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

  it("keeps the last good status when a single poll fails", async () => {
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

      // The next poll fails outright — a transient blip, or the
      // AbortController firing on a slow cold-start request.
      fetchSpy.mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6_000);
      });

      // The failure is REPORTED, but the last good status must SURVIVE it.
      // The rejection-safe fetcher resolves failures as data, so without
      // stickiness SWR overwrites the good response and `classified` blanks
      // for one cadence tick — which unmounts every consumer rendering on
      // top of it (the first-run funnel most visibly, destroying whatever
      // the user had typed) and remounts it a few seconds later.
      expect(result.current.networkError).toBeDefined();
      expect(result.current.classified?.kind).toBe("container");
      expect(result.current.derived?.phase).toBe("cold_start");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps the last good status when a poll returns a server error", async () => {
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
        await vi.advanceTimersByTimeAsync(6_000);
      });

      expect(result.current.classified?.kind).toBe("container");
      expect(result.current.networkError).toBeDefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports an error when the FIRST poll is a server error and there is no last good", async () => {
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

  it("cadence: 5s while unsettled, 15s after the first 60s, 0 once ready", async () => {
    vi.useFakeTimers();
    try {
      const fetchSpy = vi.fn().mockImplementation(jsonResponder(200, contractBody()));
      global.fetch = fetchSpy;
      const { result } = renderHook(
        () => useOnboardingStatus({ api: makeApi(), enabled: true }),
        { wrapper },
      );
      await vi.waitFor(() => expect(result.current.classified).not.toBeNull());
      const callsAfterMount = fetchSpy.mock.calls.length;

      // Fast cadence: another fetch lands within ~5s.
      await vi.advanceTimersByTimeAsync(5_000);
      expect(fetchSpy.mock.calls.length).toBeGreaterThan(callsAfterMount);

      // Cross the 60s fast-window boundary — cadence should now be 15s, not 5s.
      await vi.advanceTimersByTimeAsync(60_000);
      const callsAtBoundary = fetchSpy.mock.calls.length;
      await vi.advanceTimersByTimeAsync(5_000);
      // A pure 5s cadence would have produced another call by now; the 15s
      // cadence should not have.
      expect(fetchSpy.mock.calls.length).toBe(callsAtBoundary);
      await vi.advanceTimersByTimeAsync(10_000);
      expect(fetchSpy.mock.calls.length).toBeGreaterThan(callsAtBoundary);

      // Now flip to ready — polling should stop.
      fetchSpy.mockImplementation(
        jsonResponder(200, contractBody({ lifecycle: "ready", runtime_phase: "ready", chat_available: true })),
      );
      await vi.advanceTimersByTimeAsync(15_000);
      await vi.waitFor(() => expect(result.current.derived?.phase).toBe("ready"));
      const callsAtReady = fetchSpy.mock.calls.length;
      await vi.advanceTimersByTimeAsync(60_000);
      expect(fetchSpy.mock.calls.length).toBe(callsAtReady);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps polling (does not go quiet) while parked — the poll's own wake param is what wakes it", async () => {
    vi.useFakeTimers();
    try {
      const fetchSpy = vi.fn().mockImplementation(
        jsonResponder(200, contractBody({ lifecycle: "parked", runtime_phase: "provisioning", parked: true })),
      );
      global.fetch = fetchSpy;
      const { result } = renderHook(
        () => useOnboardingStatus({ api: makeApi(), enabled: true, wake: () => true }),
        { wrapper },
      );
      await vi.waitFor(() => expect(result.current.derived?.phase).toBe("parked"));
      const callsAtParked = fetchSpy.mock.calls.length;
      await vi.advanceTimersByTimeAsync(5_000);
      expect(fetchSpy.mock.calls.length).toBeGreaterThan(callsAtParked);
      // wake=1 was appended.
      const lastUrl = fetchSpy.mock.calls.at(-1)?.[0] as string;
      expect(lastUrl).toContain("wake=1");
    } finally {
      vi.useRealTimers();
    }
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

  it("bounded loop converges after simulating occlusion-throttling: a hung fetcher times out via AbortController and the loop keeps trying until the network recovers", async () => {
    vi.useFakeTimers();
    try {
      // First attempt: NEVER settles on its own — a genuinely hung request,
      // the exact real-world "occlusion-throttling kills the tab" scenario
      // the research pinned as the actual kill vector (not a dropped SWR
      // tick). It DOES respect AbortSignal, exactly like real fetch() —
      // this is what lets the fetcher's own timeout recover it without
      // depending on the backstop.
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

      // Nothing has resolved yet — the hook must not be stuck rendering an
      // uncaught rejection (it would surface as a jsdom unhandled-rejection
      // failure, not a hang, if the fetcher weren't rejection-safe).
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      expect(result.current.classified).toBeNull();

      // Cross the fetch timeout — the AbortController fires and the hung
      // request is aborted. The fetcher must resolve (not throw) to a
      // network-error outcome, which the loop treats as recoverable, not
      // terminal. Deliberately NOT `vi.waitFor` here — under fake timers it
      // nudges the clock forward on its own to re-poll the condition, which
      // would race past the next scheduled attempt (still on the hung mock)
      // before the test gets a chance to swap in the healthy one below.
      // Pure `advanceTimersByTimeAsync` keeps the timeline fully explicit.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(9_500);
      });
      expect(hungRejected).toBe(true);
      expect(result.current.networkError).toBeDefined();
      expect(result.current.classified).toBeNull();

      // The network recovers on a LATER attempt — the bounded loop must
      // still be scheduling one (it did not die with the hung request). The
      // window is generous (well past the single 5s fast-cadence step) since
      // the backstop may itself have re-issued one more hung attempt (still
      // on the old mock, raced against this test swapping it) that needs its
      // own AbortController timeout to clear before a healthy attempt lands
      // — the declared ceiling this test is proving, not a tight bound.
      fetchSpy.mockImplementation(() => Promise.resolve(jsonResponse(200, contractBody())));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(25_000);
      });
      expect(result.current.classified?.kind).toBe("container");
      expect(result.current.networkError).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps its cadence when a consumer re-renders faster than the poll interval", async () => {
    vi.useFakeTimers();
    try {
      const fetchSpy = vi.fn().mockImplementation(jsonResponder(200, contractBody()));
      global.fetch = fetchSpy;
      // The web stepper ticks an elapsed counter once a second for the whole
      // cold start. SWR's polling effect lists `refreshInterval` in its deps
      // and clears the pending tick on cleanup, so an unstable identity there
      // re-armed the 5s timer every second and it never fired — leaving the
      // status poll running at whatever the backstop rescued.
      const { result } = renderHook(
        () => {
          const [, setTick] = React.useState(0);
          React.useEffect(() => {
            const id = setInterval(() => setTick((t) => t + 1), 1_000);
            return () => clearInterval(id);
          }, []);
          return useOnboardingStatus({ api: makeApi(), enabled: true });
        },
        { wrapper },
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      expect(result.current.derived?.phase).toBe("cold_start");

      const before = fetchSpy.mock.calls.length;
      // Deliberately NOT inside act(): act batches the 1s tick's re-renders,
      // which is precisely the render churn this test exists to expose.
      await vi.advanceTimersByTimeAsync(30_000);
      // 30s at the 5s fast cadence is ~6 polls. The backstop alone (it only
      // fires once an attempt is cadence+3s stale) yields ~3.
      expect(fetchSpy.mock.calls.length - before).toBeGreaterThanOrEqual(5);
    } finally {
      vi.useRealTimers();
    }
  });

  it("refresh() forces an immediate revalidation and resets the fast-poll window", async () => {
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

  it("pauses the backstop while the injected visibility adapter reports hidden, and resumes on visible", async () => {
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

      // Hidden: the backstop must not force extra fetches. (SWR's own
      // refreshInterval keeps running regardless of this adapter — only the
      // raw backstop is gated — so allow it, but visibility flip + resume
      // must fire an immediate one on transition to visible.)
      visible = true;
      onChangeRef.current?.();
      await vi.waitFor(() => expect(fetchSpy.mock.calls.length).toBeGreaterThan(callsWhileHidden));
    } finally {
      vi.useRealTimers();
    }
  });
});
