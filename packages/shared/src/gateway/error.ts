/**
 * Thrown when a gateway RPC frame comes back `ok: false`.
 *
 * OpenClaw already describes its own failures structurally — see `errorShape`
 * in the fork's packages/gateway-protocol/src/schema/error-codes.ts:
 * `{ code, message, details?, retryable?, retryAfterMs? }` — and the backend
 * forwards that object verbatim (websocket_chat.py `error_obj`). So the
 * client's job is to KEEP that shape, not flatten it.
 *
 * It used to flatten it: code and details were string-joined into the Error
 * message while `retryable`/`retryAfterMs` were dropped on the floor. A
 * rebuilding session index (retryable, retryAfterMs 250) therefore reached
 * TestFlight users as the literal text
 *
 *     session history is rebuilding; retry shortly — [UNAVAILABLE] — {"method":"chat.history"}
 *
 * with a manual Retry link, for a condition that clears in a quarter second.
 * `message` is the human sentence and nothing else; everything the wire
 * carried stays reachable on `raw` for debugging.
 */
export class GatewayRpcError extends Error {
  /** Canonical gateway code, e.g. "UNAVAILABLE" / "INVALID_REQUEST". */
  readonly code?: string;
  /** True when the responder says this condition clears on its own. */
  readonly retryable: boolean;
  /** Responder's suggested wait before retrying, in ms. */
  readonly retryAfterMs?: number;
  /** The untouched wire error object — details, issues, anything else. */
  readonly raw: unknown;

  constructor(wireError: unknown) {
    const e = (wireError ?? {}) as Record<string, unknown>;
    // Only the FALLBACK carries the code: a real message is shown to users
    // verbatim and must stay clean, but "RPC call failed" with nothing else is
    // untriageable — the code is the only handle left.
    const code = typeof e.code === "string" && e.code ? e.code : undefined;
    const message =
      typeof e.message === "string" && e.message
        ? e.message
        : code
          ? `RPC call failed [${code}]`
          : "RPC call failed";
    super(message);
    this.name = "GatewayRpcError";
    this.code = typeof e.code === "string" ? e.code : undefined;
    this.retryable = e.retryable === true;
    this.retryAfterMs =
      typeof e.retryAfterMs === "number" && e.retryAfterMs > 0 ? e.retryAfterMs : undefined;
    this.raw = wireError;
  }
}

/**
 * True when the failure is a loading state wearing an error's clothes — a
 * waking container, a rebuilding index.
 *
 * Duck-typed rather than `instanceof`: this predicate is called from the
 * Expo app and the Next bundle, and a bundler that ends up with two copies
 * of this module would silently break an identity check. The `retryable`
 * field is the contract; the class is just how we build it.
 */
export function isRetryableRpcError(err: unknown): boolean {
  return (
    err instanceof Error && (err as { retryable?: unknown }).retryable === true
  );
}

/** Floor/ceiling for a responder-supplied retry delay, mirroring OpenClaw's own
 *  consumer of this field (src/tui/gateway-chat.ts). The value is chosen by
 *  whoever answered — today the gateway says 250ms and the backend says 3000ms
 *  for the same client code path — so it is a hint, not an instruction. An
 *  unclamped large value would pin a bare skeleton on screen for its whole
 *  duration with nothing to explain the wait. */
const RETRY_DELAY_MIN_MS = 100;
const RETRY_DELAY_MAX_MS = 5_000;

/** The responder's suggested retry delay (clamped), or `fallbackMs` when it
 *  gave none. */
export function rpcRetryDelayMs(err: unknown, fallbackMs: number): number {
  const ms = (err as { retryAfterMs?: unknown } | null | undefined)?.retryAfterMs;
  if (typeof ms !== "number" || !(ms > 0)) return fallbackMs;
  return Math.min(Math.max(ms, RETRY_DELAY_MIN_MS), RETRY_DELAY_MAX_MS);
}
