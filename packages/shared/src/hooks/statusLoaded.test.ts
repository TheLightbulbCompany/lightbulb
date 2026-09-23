// @vitest-environment jsdom
/**
 * The "status loaded" signal — a `GET /container/status` 200 body, republished by
 * the one hook that reads that endpoint so leaf surfaces can tell "a runtime
 * exists for this workspace" without opening a second poll.
 *
 * The scoping tests are the load-bearing ones (review fix round 1, C1): a workspace
 * switch is client-side, so this module survives it, and an unscoped value would be
 * served for the WRONG workspace until the new status fetch resolved.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import React from "react";

import {
  __resetStatusLoaded,
  publishStatusLoaded,
  setStatusScope,
  useStatusLoaded,
} from "./containerStatus";
import { useOnboardingStatus } from "./useOnboardingStatus";

function wrapper({ children }: { children: React.ReactNode }) {
  return React.createElement(SWRConfig, { value: { provider: () => new Map() } }, children);
}

function status200(body: unknown): () => Promise<Response> {
  return () =>
    Promise.resolve(
      new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }),
    );
}

function api() {
  return { apiUrl: "https://api.test", getToken: vi.fn().mockResolvedValue("tok") };
}

afterEach(() => {
  cleanup();
  __resetStatusLoaded();
  vi.restoreAllMocks();
});

describe("useStatusLoaded", () => {
  it("is false until a status body lands", () => {
    const { result } = renderHook(() => useStatusLoaded());
    // Callers must be able to tell "not known yet" from a real answer, so that a
    // surface which would wake a microVM can decline to act on a guess.
    expect(result.current).toBe(false);
  });

  it("re-renders subscribers when a status is published for the scope in view", () => {
    const { result } = renderHook(() => useStatusLoaded());
    act(() => setStatusScope("org-a"));
    act(() => publishStatusLoaded("org-a"));
    expect(result.current).toBe(true);
  });

  it("reports not loaded, not the old answer, the moment the scope changes", () => {
    const { result, rerender } = renderHook(() => useStatusLoaded());
    act(() => setStatusScope("org-a"));
    act(() => publishStatusLoaded("org-a"));
    expect(result.current).toBe(true);

    // The switch itself — no new body has landed for org B yet.
    act(() => setStatusScope("org-b"));
    rerender();
    expect(result.current).toBe(false);

    act(() => publishStatusLoaded("org-b"));
    expect(result.current).toBe(true);
  });

  it("ignores a body that resolves for the workspace we just left", () => {
    act(() => setStatusScope("org-a"));
    const { result, rerender } = renderHook(() => useStatusLoaded());
    act(() => setStatusScope("org-b"));
    // org A's in-flight poll finally answers — it must not become org B's answer.
    act(() => publishStatusLoaded("org-a"));
    rerender();
    expect(result.current).toBe(false);
  });

  it("is fed by the one /container/status reader, so a leaf needs no api config", async () => {
    global.fetch = vi.fn().mockImplementation(
      status200({ container_status: "running", lifecycle: "ready" }),
    ) as unknown as typeof fetch;

    const { result } = renderHook(
      () => {
        useOnboardingStatus({ api: api(), enabled: true });
        return useStatusLoaded();
      },
      { wrapper },
    );

    await waitFor(() => expect(result.current).toBe(true));
  });

  it("goes false across a workspace switch and stays false until the new body lands", async () => {
    // The C1 scenario end to end: the switch is client-side, so this module never
    // reloads. Serving org A's answer to org B — whose runtime may not exist yet —
    // would fire a ~55s microVM cold start on a guess.
    const fetchMock = vi
      .fn()
      .mockImplementation(
        status200({ container_status: "running", lifecycle: "ready" }),
      );
    global.fetch = fetchMock as unknown as typeof fetch;

    const { result, rerender } = renderHook(
      ({ scope }) => {
        useOnboardingStatus({ api: api(), enabled: true, scope });
        return useStatusLoaded();
      },
      { wrapper, initialProps: { scope: "org-a" } },
    );
    await waitFor(() => expect(result.current).toBe(true));

    // Synchronous with the switch — org B's fetch has not resolved yet, and the ONLY correct
    // answer in that window is "unknown". This is the assertion C1 was about.
    rerender({ scope: "org-b" });
    expect(result.current).toBe(false);

    await waitFor(() => expect(result.current).toBe(true));
  });

  it("reports not loaded while the poll is disabled (signed out)", async () => {
    const { result, rerender } = renderHook(
      ({ enabled }) => {
        useOnboardingStatus({ api: api(), enabled, scope: "org-a" });
        return useStatusLoaded();
      },
      { wrapper, initialProps: { enabled: true } },
    );
    act(() => publishStatusLoaded("/container/status|org-a"));
    expect(result.current).toBe(true);

    rerender({ enabled: false });
    expect(result.current).toBe(false);
  });
});
