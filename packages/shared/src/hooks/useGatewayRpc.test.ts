// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

import { GatewayRpcError } from "../gateway/error";
import {
  __resetPushRevalidateThrottle,
  useGatewayRpc,
  type GatewayRpcSource,
} from "./useGatewayRpc";

/**
 * Two free event wins (REALTIME, spec PR-7):
 *  1. `status_change` (the surviving gateway connect/disconnect push)
 *     revalidates a mounted `health` RPC — restores the health event/method
 *     mapping severed when PR #245 stopped forwarding OpenClaw's raw
 *     `health` heartbeat to the frontend.
 *  2. `config.changed` (already raw-forwarded to every browser) revalidates
 *     every `config.get` mount.
 * Plus the pre-existing exact/prefix match must keep working unchanged.
 */

function makeGateway(): GatewayRpcSource & { emit: (event: string, data?: unknown) => void } {
  const handlers = new Set<(event: string, data: unknown) => void>();
  return {
    sendReq: vi.fn(async () => ({ ok: true })),
    isParked: false,
    onEvent: (handler) => {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },
    emit(event, data) {
      for (const h of handlers) h(event, data);
    },
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  __resetPushRevalidateThrottle();
});

describe("useGatewayRpc event-driven revalidation", () => {
  it("revalidates a health mount on a status_change push", async () => {
    const gw = makeGateway();
    const { result } = renderHook(() => useGatewayRpc(gw, "health"));
    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(1));

    gw.emit("status_change", { state: "HEALTHY" });

    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(2));
    expect(result.current).toBeTruthy();
  });

  it("revalidates every config.get mount on a config.changed push", async () => {
    const gw = makeGateway();
    renderHook(() => useGatewayRpc(gw, "config.get"));
    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(1));

    gw.emit("config.changed", {});

    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(2));
  });

  it("still matches an event/method exact name (unrelated to the alias table)", async () => {
    const gw = makeGateway();
    renderHook(() => useGatewayRpc(gw, "agents.list"));
    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(1));

    gw.emit("agents.list", {});

    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(2));
  });

  it("does not revalidate an unrelated mount on an aliased event", async () => {
    const gw = makeGateway();
    // Distinct method from the other tests — SWR's cache is a global
    // module-level Map, so reusing "agents.list" here would dedupe against
    // an earlier test's already-cached key instead of issuing a fresh fetch.
    renderHook(() => useGatewayRpc(gw, "sessions.list"));
    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(1));

    gw.emit("status_change", { state: "HEALTHY" });
    gw.emit("config.changed", {});

    // Give any spurious revalidation a tick to land, then assert it didn't.
    await new Promise((r) => setTimeout(r, 10));
    expect(gw.sendReq).toHaveBeenCalledTimes(1);
  });

  it("revalidates a name-unrelated mount on onboarding_status_changed", async () => {
    const gw = makeGateway();
    // Reproduce the production precondition rather than a plain success: the
    // first read lands mid-reprovision, when the container has no task IP.
    // The fetcher swallows "No container" into `undefined`, so SWR caches a
    // SUCCESSFUL empty value and the onErrorRetry ladder is never armed —
    // which is exactly why nothing else ever refetches this key.
    gw.sendReq = vi
      .fn()
      .mockRejectedValueOnce(new Error("No container for user"))
      .mockResolvedValue({ ok: true });

    // Distinct method again — see the global-SWR-cache note above.
    const { result } = renderHook(() => useGatewayRpc(gw, "commands.list"));
    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(1));
    expect(result.current.data).toBeUndefined();

    // The container becoming reachable again is the only signal that recovers
    // that cached-empty value.
    gw.emit("onboarding_status_changed", {});

    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(result.current.data).toEqual({ ok: true }));
  });

  it("fans one push out to ONE rpc no matter how many mounts share the key", async () => {
    const gw = makeGateway();
    // Distinct method again — see the global-SWR-cache note above.
    renderHook(() => {
      useGatewayRpc(gw, "models.list");
      useGatewayRpc(gw, "models.list");
      useGatewayRpc(gw, "models.list");
    });
    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(1));

    gw.emit("onboarding_status_changed", {});
    await waitFor(() => expect(gw.sendReq).toHaveBeenCalledTimes(2));
    // A second push inside the throttle window is dropped, and the three
    // mounts never became three forced mutate()s (mutate bypasses dedupe).
    gw.emit("onboarding_status_changed", {});
    await new Promise((r) => setTimeout(r, 50));
    expect(gw.sendReq).toHaveBeenCalledTimes(2);
  });
});

describe("useGatewayRpc error-retry ladder", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("stops retrying when the gateway ANSWERED and said the condition will not clear", async () => {
    // Prod, 8.1 walk: `sessions.search` came back INVALID_REQUEST "agentId
    // requires sessionKeys" and the fixed 3s ladder re-fired the identical
    // rejected call for minutes — the backend log carried 12+ in one minute.
    // The responder has told us the answer will not change.
    vi.useFakeTimers();
    const gw = makeGateway();
    gw.sendReq = vi.fn().mockRejectedValue(
      new GatewayRpcError({
        code: "INVALID_REQUEST",
        message: "agentId requires sessionKeys",
      }),
    );

    renderHook(() => useGatewayRpc(gw, "sessions.search", { q: "terminal" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(gw.sendReq).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(gw.sendReq).toHaveBeenCalledTimes(1);
  });

  it("keeps the ladder for a failure the gateway never answered (timeout, dead socket)", async () => {
    vi.useFakeTimers();
    const gw = makeGateway();
    gw.sendReq = vi.fn().mockRejectedValue(new Error("RPC timeout"));

    renderHook(() => useGatewayRpc(gw, "sessions.search", { q: "transport" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(gw.sendReq).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect((gw.sendReq as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(1);
  });

  it("keeps the ladder for a gateway error the responder marked retryable", async () => {
    vi.useFakeTimers();
    const gw = makeGateway();
    gw.sendReq = vi.fn().mockRejectedValue(
      new GatewayRpcError({
        code: "UNAVAILABLE",
        message: "session history is rebuilding; retry shortly",
        retryable: true,
      }),
    );

    renderHook(() => useGatewayRpc(gw, "sessions.search", { q: "rebuilding" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(gw.sendReq).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect((gw.sendReq as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(1);
  });
});
