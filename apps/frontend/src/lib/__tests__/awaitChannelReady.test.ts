import { describe, it, expect, vi } from "vitest";

import { awaitChannelReady } from "../awaitChannelReady";

type Snap = {
  running?: boolean;
  connected?: boolean;
  lastError?: string | null;
  lastStartAt?: number | null;
};

/** A channels.status stub: returns the next scripted snapshot per call.
 * The literal string "throw" simulates the RPC failing (gateway mid-restart).
 * `null` simulates the account not being present yet. The last entry repeats. */
function scriptedCallRpc(provider: string, accountId: string, script: Array<Snap | "throw" | null>) {
  let i = 0;
  return vi.fn(async () => {
    const entry = script[Math.min(i, script.length - 1)];
    i++;
    if (entry === "throw") throw new Error("gateway restarting");
    const accounts = entry == null ? [] : [{ accountId, ...entry }];
    return { channelAccounts: { [provider]: accounts } };
  });
}

/** Monotonic clock: returns 0,1,2,… one tick per call. */
function makeClock() {
  let t = 0;
  return () => t++;
}

const BASE = {
  provider: "telegram",
  accountId: "a",
  initialStartAt: 100 as number | null,
  sleep: async () => {},
};

describe("awaitChannelReady", () => {
  it("resolves ready when the restarted account reports connected", async () => {
    const callRpc = scriptedCallRpc("telegram", "a", [
      { running: true, lastStartAt: 100 }, // pre-PATCH echo (not a restart)
      { running: true, lastStartAt: 200, lastError: null }, // restarted, auth not done yet
      { running: true, lastStartAt: 200, connected: true }, // connected!
    ]);
    const out = await awaitChannelReady({ ...BASE, callRpc, now: makeClock(), deadlineMs: 1000 });
    expect(out).toEqual({ status: "ready" });
  });

  it("does NOT treat bare running:true (pre-auth race) as success", async () => {
    // running:true with no connected and no error must NOT resolve ready —
    // the gateway sets running:true before auth runs (server-channels.ts:584).
    const callRpc = scriptedCallRpc("telegram", "a", [
      { running: true, lastStartAt: 200, lastError: null },
    ]);
    const out = await awaitChannelReady({ ...BASE, callRpc, now: makeClock(), deadlineMs: 5 });
    expect(out.status).toBe("timeout"); // never connected → times out, not ready
  });

  it("fails fast with the real error when the restarted account reports lastError", async () => {
    const callRpc = scriptedCallRpc("telegram", "a", [
      { running: true, lastStartAt: 200, lastError: null }, // pre-auth
      { running: false, lastStartAt: 200, lastError: "getMe returned 401" }, // auth failed
    ]);
    const out = await awaitChannelReady({ ...BASE, callRpc, now: makeClock(), deadlineMs: 1000 });
    expect(out).toEqual({ status: "error", lastError: "getMe returned 401" });
  });

  it("keeps waiting through a deferred restart (lastStartAt never advances) until deadline", async () => {
    // Busy box: reload deferred, account snapshot never gets a newer lastStartAt.
    const callRpc = scriptedCallRpc("telegram", "a", [{ running: true, lastStartAt: 100 }]);
    const notes: string[] = [];
    const out = await awaitChannelReady({
      ...BASE,
      callRpc,
      now: makeClock(),
      deadlineMs: 5,
      onNote: (n) => notes.push(n),
    });
    expect(out.status).toBe("timeout");
    // While the restart hasn't landed, the user sees an "applying" note, not a frozen spinner.
    expect(notes.some((n) => /applying/i.test(n))).toBe(true);
  });

  it("tolerates channels.status throwing while the gateway restarts", async () => {
    const callRpc = scriptedCallRpc("telegram", "a", [
      "throw",
      "throw",
      { running: true, lastStartAt: 200, connected: true },
    ]);
    const notes: string[] = [];
    const out = await awaitChannelReady({
      ...BASE,
      callRpc,
      now: makeClock(),
      deadlineMs: 1000,
      onNote: (n) => notes.push(n),
    });
    expect(out).toEqual({ status: "ready" });
    expect(notes.some((n) => /restart/i.test(n))).toBe(true);
  });

  it("bails as cancelled (non-destructive) when shouldCancel returns true", async () => {
    const callRpc = scriptedCallRpc("telegram", "a", [{ running: true, lastStartAt: 100 }]);
    const out = await awaitChannelReady({
      ...BASE,
      callRpc,
      now: makeClock(),
      deadlineMs: 1000,
      shouldCancel: () => true,
    });
    expect(out).toEqual({ status: "cancelled" });
  });
});
