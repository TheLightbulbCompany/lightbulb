import type { IncomingMessage, ServerResponse } from "node:http";

export const RPC_ROUTE = "/plugins/lightbulb-channel/rpc";
export const DEFAULT_TIMEOUT_MS = 30_000;
export const MAX_TIMEOUT_MS = 120_000;
const MAX_BODY_BYTES = 1_048_576;

type DispatchError = { code: string; message: string };
type Dispatch = (method: string, params?: unknown, options?: { timeoutMs?: number }) => Promise<{ ok: boolean; payload?: unknown; error?: DispatchError }>;
type ReadBody = (params: { req: IncomingMessage; res: ServerResponse; maxBytes: number; timeoutMs: number; invalidJsonMessage: string }) => Promise<{ ok: boolean; value?: unknown }>;

const UNAVAILABLE: DispatchError = { code: "UNAVAILABLE", message: "gateway method failed before returning a response" };

function json(res: ServerResponse, status: number, body: Record<string, unknown>): true {
  res.statusCode = status;
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(body));
  return true;
}

/** Backend → gateway control-plane RPC over HTTP; the backend's allowlists stay the policy edge. */
export function createRpcHandler(deps: { dispatch: Dispatch; readBody: ReadBody }) {
  return async (req: IncomingMessage, res: ServerResponse): Promise<boolean> => {
    if ((req.method ?? "GET").toUpperCase() !== "POST") {
      res.setHeader("Allow", "POST");
      return json(res, 405, { ok: false, error: { code: "INVALID_REQUEST", message: "POST only" } });
    }
    const body = await deps.readBody({ req, res, maxBytes: MAX_BODY_BYTES, timeoutMs: 10_000, invalidJsonMessage: "invalid JSON body" });
    if (!body.ok) return true;
    const value = (body.value ?? {}) as Record<string, unknown>;
    const method = typeof value.method === "string" ? value.method.trim() : "";
    const params = value.params ?? {};
    if (!method || typeof params !== "object" || params === null || Array.isArray(params)) {
      return json(res, 400, { ok: false, error: { code: "INVALID_REQUEST", message: "method must be a non-empty string and params an object" } });
    }
    const requested = typeof value.timeoutMs === "number" && Number.isFinite(value.timeoutMs) && value.timeoutMs > 0 ? value.timeoutMs : DEFAULT_TIMEOUT_MS;
    let response;
    try {
      response = await deps.dispatch(method, params, { timeoutMs: Math.min(requested, MAX_TIMEOUT_MS) });
    } catch {
      return json(res, 200, { ok: false, error: UNAVAILABLE }); // never the exception text: it may carry params
    }
    if (response.ok) return json(res, 200, { ok: true, payload: response.payload ?? null });
    const error = response.error ?? UNAVAILABLE;
    return json(res, error.code === "AGENT_TIMEOUT" ? 504 : 200, { ok: false, error });
  };
}
