import { useCallback, useEffect } from "react";
import useSWR, { type SWRConfiguration } from "swr";

import type { GatewayConnectionValue } from "../gateway/useGatewayConnection";

/** The slice of the gateway connection the RPC hooks actually need. */
export type GatewayRpcSource = Pick<
  GatewayConnectionValue,
  "sendReq" | "onEvent" | "isParked"
>;

/**
 * Event -> method-prefix aliases for revalidation matching (spec PR-7's two
 * free event wins).
 *
 *  - `status_change`: the surviving gateway connect/disconnect push (see
 *    GatewayConnection._emit_status_change) — restores the `health`
 *    event/method mapping severed when #245 stopped forwarding OpenClaw's
 *    raw `health` heartbeat to the frontend.
 *  - `config.changed`: already raw-forwarded to every browser today and
 *    consumed by nobody — this one line gives every `config.get` mount real
 *    push freshness.
 *
 * The exact/prefix match below (`method === event || method.startsWith(event
 * + ".")`) still runs unaliased for every other event; this table only adds
 * extra event names a method should ALSO revalidate on.
 */
const EVENT_METHOD_ALIASES: Record<string, string[]> = {
  status_change: ["health"],
  "config.changed": ["config.get"],
};

/**
 * Events that revalidate EVERY gateway-backed read, not just name-matched
 * methods.
 *
 * `onboarding_status_changed` (backend `core/services/status_events.py`) fires
 * after every durable write `compute_status` composes — including the
 * containers-row transition when a container becomes reachable again after a
 * cold reprovision (a BYO-key provider switch, a resize, an admin
 * reprovision).
 *
 * That transition needs a blanket revalidate because reads taken while the
 * container had no IP do not fail: the fetcher below deliberately swallows
 * "No container" into `undefined`, so SWR caches a SUCCESSFUL empty value and
 * `onErrorRetry` never fires. Nothing else refetches those keys, so an open
 * tab kept an empty agent roster until a manual reload, with the provisioning
 * stepper parked on the phase that reads it. This event is the one signal
 * that the container is reachable again. The 10s throttle at the call site
 * bounds the fan-out.
 */
const REVALIDATE_ALL_EVENTS: ReadonlySet<string> = new Set([
  "onboarding_status_changed",
]);

function eventMatchesMethod(method: string, event: string): boolean {
  if (REVALIDATE_ALL_EVENTS.has(event)) return true;
  if (method === event || method.startsWith(event + ".")) return true;
  const aliasTargets = EVENT_METHOD_ALIASES[event];
  if (!aliasTargets) return false;
  return aliasTargets.some(
    (target) => method === target || method.startsWith(target + "."),
  );
}

export interface RpcResult<T = unknown> {
  data: T | undefined;
  error: Error | undefined;
  isLoading: boolean;
  mutate: () => Promise<T | undefined>;
}

/**
 * Read-only RPC calls via the gateway WebSocket (auto-fetched via SWR).
 *
 * Extracted from the web frontend's useGatewayRpc (Phase 1); the platform
 * wrapper supplies its gateway context value as `gateway` — the rest is
 * shared behavior (SWR keying, fixed-cadence cold-start retry, event-driven
 * revalidation with throttling).
 */
/** The SWR key a `useGatewayRpc(method, params)` mount reads from. */
export function gatewayRpcKey(method: string, params?: Record<string, unknown>): string {
  return `rpc|${method}|${params ? JSON.stringify(params) : ""}`;
}

// Push-driven revalidation is throttled PER KEY, module-wide — not per hook
// instance. `mutate()` bypasses SWR's dedupingInterval, and a roster key is
// mounted by every sidebar row, chat panel and control panel at once, so a
// per-instance throttle still let one gateway push fan out into one forced
// RPC per mounted instance. On a container whose backend socket was flapping
// that was ~350 identical `agents.list` calls in 16 minutes from two
// browsers — load that fed the very starvation causing the flaps.
const PUSH_REVALIDATE_MIN_MS = 10_000;
const lastPushRevalidateAt = new Map<string, number>();

/** Revalidate `key` in response to a gateway push, at most once per 10s per key. */
export function pushRevalidate(key: string, mutate: () => unknown): void {
  const now = Date.now();
  if (now - (lastPushRevalidateAt.get(key) ?? 0) < PUSH_REVALIDATE_MIN_MS) return;
  lastPushRevalidateAt.set(key, now);
  void mutate();
}

/** Test-only: forget every key's last push-revalidate time. */
export function __resetPushRevalidateThrottle(): void {
  lastPushRevalidateAt.clear();
}

export function useGatewayRpc<T = unknown>(
  gateway: GatewayRpcSource,
  method: string | null,
  params?: Record<string, unknown>,
  config?: SWRConfiguration,
): RpcResult<T> {
  const { sendReq, onEvent, isParked } = gateway;

  const fetcher = useCallback(
    async (key: string) => {
      // Split on first two "|" only — params JSON may contain pipe characters
      const firstPipe = key.indexOf("|");
      const secondPipe = key.indexOf("|", firstPipe + 1);
      const m = key.slice(firstPipe + 1, secondPipe);
      const paramStr = key.slice(secondPipe + 1);
      const parsedParams = paramStr ? JSON.parse(paramStr) : undefined;
      try {
        return (await sendReq(m, parsedParams)) as T;
      } catch (err) {
        // Match old behavior: 404-equivalent returns undefined
        if (err instanceof Error && err.message.includes("No container")) {
          return undefined;
        }
        throw err;
      }
    },
    [sendReq],
  );

  // Null the SWR key while the socket is deliberately parked: a parked
  // connection has no live WS, so any read RPC would reject and — because
  // onErrorRetry below retries at a fixed 3s cadence up to ~5min — every
  // gateway-backed component that renders over a parked box (the chat
  // surface stays mounted by design) would enter a retry storm against the
  // closed socket. Nulling the key freezes each RPC at its last cached value
  // and stops revalidation; unpark() flips isParked false and SWR resumes.
  // This never triggers a wake (RPCs don't touch /container/*), so park is
  // preserved.
  const swrKey = method && !isParked ? gatewayRpcKey(method, params) : null;

  const { data, error, isLoading, mutate } = useSWR<T | undefined>(
    swrKey as string | null,
    fetcher as (key: string) => Promise<T | undefined>,
    {
      revalidateOnFocus: false,
      dedupingInterval: 10000,
      // SWR's default `onErrorRetry` does exponential backoff
      // (5s, 10s, 20s, 40s...). Bad for cold-start probing: a single
      // handshake-timeout on the gateway pool during the openclaw
      // sidecar window pushes the next probe minutes out, even though
      // the gateway becomes healthy ~10s later — user gets stuck on
      // the provisioning stepper waiting for the long-since-recovered
      // gateway. Retry at a fixed cadence instead, capped so we don't
      // poll forever on a permanently broken container.
      errorRetryInterval: 3000,
      onErrorRetry: (_err, _key, swrCfg, revalidate, opts) => {
        const retryCount = opts?.retryCount ?? 0;
        if (retryCount >= 100) return; // ~5 min at 3s
        const delay =
          typeof swrCfg.refreshInterval === "number" && swrCfg.refreshInterval > 0
            ? swrCfg.refreshInterval
            : swrCfg.errorRetryInterval ?? 3000;
        setTimeout(() => revalidate({ retryCount: retryCount + 1 }), delay);
      },
      ...config,
    },
  );

  // Auto-revalidate when gateway pushes a matching event, through the
  // per-key throttle above (a parked mount has no key and stays frozen).
  useEffect(() => {
    if (!method || !swrKey) return;
    return onEvent((event) => {
      if (eventMatchesMethod(method, event)) pushRevalidate(swrKey, mutate);
    });
  }, [method, swrKey, onEvent, mutate]);

  return {
    data,
    error: error as Error | undefined,
    isLoading,
    // Return SWR's mutate promise so callers can `await` a revalidation
    // and act only after the fresh data has been applied.
    mutate: () => mutate(),
  };
}

/**
 * Write RPC calls via the gateway WebSocket (imperative, not auto-fetched).
 */
export function useGatewayRpcMutation(gateway: Pick<GatewayConnectionValue, "sendReq">) {
  const { sendReq } = gateway;

  return useCallback(
    async <T = unknown>(
      method: string,
      params?: Record<string, unknown>,
      timeoutMs?: number,
    ): Promise<T> => {
      return (await sendReq(method, params, timeoutMs)) as T;
    },
    [sendReq],
  );
}
