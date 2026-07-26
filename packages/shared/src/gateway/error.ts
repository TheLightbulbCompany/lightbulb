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
    const message = typeof e.message === "string" && e.message ? e.message : "RPC call failed";
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

/** The responder's suggested retry delay, or `fallbackMs` when it gave none. */
export function rpcRetryDelayMs(err: unknown, fallbackMs: number): number {
  const ms = (err as { retryAfterMs?: unknown } | null | undefined)?.retryAfterMs;
  return typeof ms === "number" && ms > 0 ? ms : fallbackMs;
}
