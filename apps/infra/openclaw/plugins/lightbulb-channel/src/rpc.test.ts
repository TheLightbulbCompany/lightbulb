import { describe, expect, it, vi } from "vitest";
import { DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, RPC_ROUTE, createRpcHandler } from "./rpc.js";

function res() {
  const out: { status?: number; body?: any } = {};
  return {
    out,
    res: {
      setHeader: vi.fn(),
      end: vi.fn((text: string) => { out.body = JSON.parse(text); }),
      set statusCode(v: number) { out.status = v; },
      get statusCode() { return out.status ?? 200; },
    } as any,
  };
}
const req = (method = "POST") => ({ method, url: RPC_ROUTE } as any);
function handler(dispatch: any, body: any) {
  const readBody = vi.fn(async () => ({ ok: true, value: body }));
  return { run: createRpcHandler({ dispatch, readBody }), readBody };
}

describe("createRpcHandler", () => {
  it("dispatches and returns the payload", async () => {
    const dispatch = vi.fn(async () => ({ ok: true, payload: { agents: [] } }));
    const { run } = handler(dispatch, { method: "agents.list", params: { x: 1 }, timeoutMs: 5000 });
    const r = res();
    await run(req(), r.res);
    expect(r.out).toEqual({ status: 200, body: { ok: true, payload: { agents: [] } } });
    expect(dispatch).toHaveBeenCalledWith("agents.list", { x: 1 }, { timeoutMs: 5000 });
  });

  it("clamps timeoutMs and defaults it", async () => {
    const dispatch = vi.fn(async () => ({ ok: true, payload: null }));
    const { run } = handler(dispatch, { method: "health", timeoutMs: 999_999 });
    await run(req(), res().res);
    expect(dispatch).toHaveBeenLastCalledWith("health", {}, { timeoutMs: MAX_TIMEOUT_MS });
    const second = handler(dispatch, { method: "health" });
    await second.run(req(), res().res);
    expect(dispatch).toHaveBeenLastCalledWith("health", {}, { timeoutMs: DEFAULT_TIMEOUT_MS });
  });

  it("passes gateway errors through as 200 ok:false and timeouts as 504", async () => {
    const error = { code: "NOT_FOUND", message: "no such agent" };
    const { run } = handler(vi.fn(async () => ({ ok: false, error })), { method: "agents.delete" });
    const r = res();
    await run(req(), r.res);
    expect(r.out).toEqual({ status: 200, body: { ok: false, error } });
    const t = res();
    await handler(vi.fn(async () => ({ ok: false, error: { code: "AGENT_TIMEOUT", message: "slow" } })), { method: "x" }).run(req(), t.res);
    expect(t.out.status).toBe(504);
    expect(t.out.body.error.code).toBe("AGENT_TIMEOUT");
  });

  it("rejects malformed bodies and non-POST without dispatching", async () => {
    const dispatch = vi.fn();
    for (const body of [{}, { method: "" }, { method: "x", params: [1] }, { method: 3 }]) {
      const r = res();
      await handler(dispatch, body).run(req(), r.res);
      expect(r.out.status).toBe(400);
      expect(r.out.body.error.code).toBe("INVALID_REQUEST");
    }
    const r = res();
    await handler(dispatch, { method: "x" }).run(req("GET"), r.res);
    expect(r.out.status).toBe(405);
    expect(dispatch).not.toHaveBeenCalled();
  });

  it("maps a throwing dispatch to 200 ok:false UNAVAILABLE", async () => {
    const { run } = handler(vi.fn(async () => { throw new Error("boom"); }), { method: "x" });
    const r = res();
    await run(req(), r.res);
    expect(r.out).toEqual({ status: 200, body: { ok: false, error: { code: "UNAVAILABLE", message: "gateway method failed before returning a response" } } });
  });
});
