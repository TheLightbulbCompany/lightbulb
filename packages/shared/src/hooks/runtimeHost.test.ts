// @vitest-environment jsdom
/**
 * The runtime capability signal (M6 task 10, row 0) — `GET /container/status`'s
 * `runtime_host`, republished by the one hook that reads that endpoint so leaf
 * surfaces can branch without opening a second poll.
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
  __resetRuntimeHost,
  publishRuntimeHost,
  setRuntimeHostScope,
  useRuntimeHost,
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
  __resetRuntimeHost();
  vi.restoreAllMocks();
});

describe("useRuntimeHost", () => {
  it("is undefined until a status body names a host", () => {
    const { result } = renderHook(() => useRuntimeHost());
    // NOT "ecs": callers must be able to tell "not known yet" from a real answer, so that a
    // surface which would wake a microVM can decline to act on a guess.
    expect(result.current).toBeUndefined();
  });

  it("re-renders subscribers when the host is published for the scope in view", () => {
    const { result } = renderHook(() => useRuntimeHost());
    act(() => setRuntimeHostScope("org-a"));
    act(() => publishRuntimeHost("org-a", "agentcore"));
    expect(result.current).toBe("agentcore");
    act(() => publishRuntimeHost("org-a", "ecs"));
    expect(result.current).toBe("ecs");
  });

  it("reports unknown, not the old answer, the moment the scope changes", () => {
    const { result, rerender } = renderHook(() => useRuntimeHost());
    act(() => setRuntimeHostScope("org-a"));
    act(() => publishRuntimeHost("org-a", "agentcore"));
    expect(result.current).toBe("agentcore");

    // The switch itself — no new body has landed for org B yet.
    act(() => setRuntimeHostScope("org-b"));
    rerender();
    expect(result.current).toBeUndefined();

    act(() => publishRuntimeHost("org-b", "ecs"));
    expect(result.current).toBe("ecs");
  });

  it("ignores a body that resolves for the workspace we just left", () => {
    act(() => setRuntimeHostScope("org-a"));
    const { result, rerender } = renderHook(() => useRuntimeHost());
    act(() => setRuntimeHostScope("org-b"));
    // org A's in-flight poll finally answers — it must not become org B's answer.
    act(() => publishRuntimeHost("org-a", "agentcore"));
    rerender();
    expect(result.current).toBeUndefined();
  });

  it("is fed by the one /container/status reader, so a leaf needs no api config", async () => {
    global.fetch = vi.fn().mockImplementation(
      status200({ container_status: "running", lifecycle: "ready", runtime_host: "agentcore" }),
    ) as unknown as typeof fetch;

    const { result } = renderHook(
      () => {
        useOnboardingStatus({ api: api(), enabled: true });
        return useRuntimeHost();
      },
      { wrapper },
    );

    await waitFor(() => expect(result.current).toBe("agentcore"));
  });

  it("goes unknown across a workspace switch and stays unknown until the new body lands", async () => {
    // The C1 scenario end to end: org A is AgentCore, org B is ECS, and the switch is
    // client-side, so this module never reloads. Serving org A's "agentcore" to org B would
    // hide a working Tools tab; the reverse would fire a ~55s microVM cold start.
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(
        status200({ container_status: "running", lifecycle: "ready", runtime_host: "agentcore" }),
      )
      .mockImplementation(status200({ container_status: "running", lifecycle: "ready", runtime_host: "ecs" }));
    global.fetch = fetchMock as unknown as typeof fetch;

    const { result, rerender } = renderHook(
      ({ scope }) => {
        useOnboardingStatus({ api: api(), enabled: true, scope });
        return useRuntimeHost();
      },
      { wrapper, initialProps: { scope: "org-a" } },
    );
    await waitFor(() => expect(result.current).toBe("agentcore"));

    // Synchronous with the switch — org B's fetch has not resolved yet, and the ONLY correct
    // answer in that window is "unknown". This is the assertion C1 was about.
    rerender({ scope: "org-b" });
    expect(result.current).toBeUndefined();

    await waitFor(() => expect(result.current).toBe("ecs"));
  });

  it("a status body without the field leaves the last known host alone", async () => {
    // Within ONE workspace an older backend (or a 402/404 poll) must not blank a host we
    // already know — blanking silently re-enables every ECS-only surface on a migrated
    // workspace. Going unknown is a SWITCH's job, not a poll's.
    global.fetch = vi.fn().mockImplementation(
      status200({ container_status: "running", lifecycle: "ready" }),
    ) as unknown as typeof fetch;

    const { result } = renderHook(
      () => {
        const status = useOnboardingStatus({ api: api(), enabled: true, scope: "org-a" });
        return { host: useRuntimeHost(), kind: status.classified?.kind };
      },
      { wrapper },
    );
    // Publish AFTER the hook has declared its scope, mimicking an earlier poll of this same
    // workspace that did carry the field.
    act(() => publishRuntimeHost("/container/status|org-a", "agentcore"));

    await waitFor(() => expect(result.current.kind).toBe("container"));
    expect(result.current.host).toBe("agentcore");
  });

  it("reports unknown while the poll is disabled (signed out)", async () => {
    const { result, rerender } = renderHook(
      ({ enabled }) => {
        useOnboardingStatus({ api: api(), enabled, scope: "org-a" });
        return useRuntimeHost();
      },
      { wrapper, initialProps: { enabled: true } },
    );
    act(() => publishRuntimeHost("/container/status|org-a", "agentcore"));
    expect(result.current).toBe("agentcore");

    rerender({ enabled: false });
    expect(result.current).toBeUndefined();
  });
});
