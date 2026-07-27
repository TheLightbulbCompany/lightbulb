import { describe, expect, it } from "vitest";

import { GatewayRpcError, isRetryableRpcError, rpcRetryDelayMs } from "./error";

describe("GatewayRpcError", () => {
  // The regression this whole module exists for: OpenClaw's rebuilding-index
  // error reached TestFlight users as
  //   "session history is rebuilding; retry shortly — [UNAVAILABLE] — {"method":"chat.history"}"
  // because the client string-joined code+details into the message and threw
  // away the retryable/retryAfterMs that would have hidden it entirely.
  it("keeps the message human and the retry metadata intact", () => {
    const err = new GatewayRpcError({
      code: "UNAVAILABLE",
      message: "session history is rebuilding; retry shortly",
      details: { method: "chat.history" },
      retryable: true,
      retryAfterMs: 250,
    });

    expect(err.message).toBe("session history is rebuilding; retry shortly");
    expect(err.message).not.toContain("UNAVAILABLE");
    expect(err.message).not.toContain("chat.history");

    expect(err.code).toBe("UNAVAILABLE");
    expect(err.retryable).toBe(true);
    expect(err.retryAfterMs).toBe(250);
    // Nothing the wire carried is lost — just moved off the user-facing text.
    expect(err.raw).toMatchObject({ details: { method: "chat.history" } });

    expect(isRetryableRpcError(err)).toBe(true);
    expect(rpcRetryDelayMs(err, 1000)).toBe(250);
  });

  it("defaults to non-retryable so an unflagged failure still surfaces", () => {
    const err = new GatewayRpcError({ message: "No container provisioned." });

    expect(err.retryable).toBe(false);
    expect(isRetryableRpcError(err)).toBe(false);
    expect(rpcRetryDelayMs(err, 1000)).toBe(1000);
  });

  it("survives a malformed or absent error object", () => {
    expect(new GatewayRpcError(undefined).message).toBe("RPC call failed");
    expect(new GatewayRpcError({ message: "" }).message).toBe("RPC call failed");
    expect(new GatewayRpcError({ retryAfterMs: -1 }).retryAfterMs).toBeUndefined();
  });

  it("does not treat a plain Error as retryable", () => {
    expect(isRetryableRpcError(new Error("Gateway connection lost"))).toBe(false);
    expect(isRetryableRpcError(null)).toBe(false);
    // A bare object is not an Error — the predicate must not match wire data.
    expect(isRetryableRpcError({ retryable: true })).toBe(false);
  });
});
