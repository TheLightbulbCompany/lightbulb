// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";

import { useGatewayRpc, type GatewayRpcSource } from "./useGatewayRpc";

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
});
