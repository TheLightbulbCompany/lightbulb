// @vitest-environment jsdom
/**
 * The runtime capability signal (M6 task 10, row 0) — `GET /container/status`'s
 * `runtime_host`, republished by the one hook that reads that endpoint so leaf
 * surfaces can branch without opening a second poll.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import React from "react";

import { __resetRuntimeHost, publishRuntimeHost, useRuntimeHost } from "./containerStatus";
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

  it("re-renders subscribers when the host is published", () => {
    const { result } = renderHook(() => useRuntimeHost());
    act(() => publishRuntimeHost("agentcore"));
    expect(result.current).toBe("agentcore");
    act(() => publishRuntimeHost("ecs"));
    expect(result.current).toBe("ecs");
  });

  it("is fed by the one /container/status reader, so a leaf needs no api config", async () => {
    global.fetch = vi.fn().mockImplementation(
      status200({ container_status: "running", lifecycle: "ready", runtime_host: "agentcore" }),
    ) as unknown as typeof fetch;

    const { result } = renderHook(
      () => {
        useOnboardingStatus({
          api: { apiUrl: "https://api.test", getToken: vi.fn().mockResolvedValue("tok") },
          enabled: true,
        });
        return useRuntimeHost();
      },
      { wrapper },
    );

    await waitFor(() => expect(result.current).toBe("agentcore"));
  });

  it("a status body without the field leaves the last known host alone", async () => {
    // An older backend (or a 402/404 poll) must not blank a host we already know — a blanked
    // host silently re-enables every ECS-only surface on a migrated workspace.
    publishRuntimeHost("agentcore");
    global.fetch = vi.fn().mockImplementation(
      status200({ container_status: "running", lifecycle: "ready" }),
    ) as unknown as typeof fetch;

    const { result } = renderHook(
      () => {
        const status = useOnboardingStatus({
          api: { apiUrl: "https://api.test", getToken: vi.fn().mockResolvedValue("tok") },
          enabled: true,
        });
        return { host: useRuntimeHost(), kind: status.classified?.kind };
      },
      { wrapper },
    );

    await waitFor(() => expect(result.current.kind).toBe("container"));
    expect(result.current.host).toBe("agentcore");
  });
});
