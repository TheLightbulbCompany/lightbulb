# Stateless Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the per-owner gateway WebSocket pool; every backend → container call becomes one HTTP request to the container plugin, readiness probes the container, trial idle reads the run ledger, non-room usage arrives over a hook-token route, and the backend can run on any number of instances.

**Architecture:** The plugin registers `POST /plugins/lightbulb-channel/rpc` through the plugin API (manifest contract `gatewayMethodDispatch`) and dispatches gateway methods in-process; `core/services/container_rpc.call` replaces `pool.send_rpc` and `gateway_rpc_oneshot` with the same exception contract, so callers change one line. An `agent_end` hook posts usage for non-room runs. Everything the pool did that has no consumer (approval/question/progress forwarding, broadcast-driven invalidation, liveness rows, self-heal dial, cron telemetry sweep) is deleted, not ported.

**Tech Stack:** OpenClaw 2026.9.3 plugin-sdk (`dispatchGatewayMethod`, `api.registerHttpRoute`, `api.on("agent_end")`, `getSessionEntry`), FastAPI + httpx, DynamoDB (`lightbulb-{env}-messaging`), CDK observability stack, vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-09-12-stateless-backend-design.md`

## Global Constraints

- No backward compatibility, no feature flags, fix-forward: there are no external users. Delete, do not port.
- Never log or emit message text, handles, or emails. The RPC route never logs `params`. Metrics never carry owner/user/room/member ids as dimensions; `method` is a dimension only because it is a closed set.
- `container_rpc.call(owner_id, method, params=None, *, timeout_s=30.0)` raises exactly what `pool.send_rpc` raised: 504 → `asyncio.TimeoutError`; `ok: false` → `RuntimeError(json.dumps(error))`; no container / transport failure → `ApiProblem("RUNTIME_UNAVAILABLE", retryable=True, retry_after=3)`. No retries inside `call`.
- Plugin RPC route: `POST /plugins/lightbulb-channel/rpc`, `auth: "gateway"`, `match: "exact"`, `gatewayRuntimeScopeSurface: "trusted-operator"`, registered via `api.registerHttpRoute` in `registerFull` (the SDK helper `registerPluginHttpRoute` never stamps dispatch permission); manifest `"contracts": {"gatewayMethodDispatch": ["authenticated-request"]}`. Body `{method, params?, timeoutMs?}`, `timeoutMs ≤ 120000`, default 30000. Answers: 200 `{ok: true, payload}`, 200 `{ok: false, error}`, 400 `INVALID_REQUEST`, 504 `AGENT_TIMEOUT`.
- Usage callback: `POST /api/v1/internal/messaging/usage/{ownerId}` (hook token), body `{sessionKey, agentId, runId, success, durationMs?, usage: {model, inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens, estimatedCostUsd}}`; room session keys are ignored; dedup by receipt row `OWNER#{ownerId}` / `USAGE#{runId}` (7-day TTL).
- `runtime_rpc.execute_sync` publishes `runtime.resource.invalidated {resource}` after a successful mutating method: `agents.update|agents.delete → agents`, `config.set|config.patch → config`, `skills.install|skills.update|skills.proposals.apply|skills.proposals.reject → skills`.
- Readiness: `gateway_connected` = `container_rpc.call(owner_id, "health", timeout_s=2.0)` succeeded, probed only for a `running`, not parked row. Trial idle: veto on `messaging_repo.has_open_runs(owner_id)`; no connection to close.
- P12 `backend-liveness` keeps `gateway.running.count`, emitted from `container_repo.get_by_status("running")` every 60 s.
- `desiredCount` stays 1 in prod (D flips it). Dev proof at 2 by a manual `update-service`, then back to 1.
- Ponytail rule: shortest working diff; one runnable check per non-trivial branch; no new abstractions beyond `container_rpc`, `chat_gate`, `session_keys`.
- Rollout is two PRs: the plugin PR (Tasks 1–2, merge builds the image) then the cutover PR (Tasks 3–10, pins + backend + infra + docs; Task 11 deploys). Never push, merge, or touch AWS without the owner's explicit approval.

---

## File structure

**Plugin (`apps/infra/openclaw/plugins/lightbulb-channel/`)**
- `openclaw.plugin.json` — gains `contracts.gatewayMethodDispatch`.
- `src/rpc.ts` (new) — `createRpcHandler({dispatch, readBody})`: body validation, timeout clamp, dispatch, error mapping.
- `src/usage.ts` (new) — `createUsageHook({post, getSessionEntry, log})`: the `agent_end` handler for non-room runs.
- `src/runs.ts` — `postToBackend(deps, path, body)` extracted from `createRunCallbacks.send` and reused; `usageFromEntry(entry)` extracted from `usageFor`.
- `src/index.ts` — `registerFull(api)` registers the route and the hook.
- `src/rpc.test.ts`, `src/usage.test.ts` (new), `src/runs.test.ts` (one assertion on the extracted helpers).
- `dist/*` — rebuilt and committed (the image bakes `dist`).

**Backend (`apps/backend/`)**
- `core/services/container_rpc.py` (new) — `running_container(owner_id)` (moved from `runtime_rpc._running_container`), `call(...)`.
- `core/services/session_keys.py` (new) — `parse_session_key`, `parse_lightbulb_session_key` (moved out of the pool).
- `core/services/chat_gate.py` (new) — `gate_chat(*, user_id, owner_id, session_key)` (moved out of the pool).
- `core/services/runtime_rpc.py` — `execute_sync` on `container_rpc`, `_invalidate`, `execute_control` deleted.
- `routers/runtime_rpc.py` — approval/question resolve routes deleted; `core/services/messaging.py` loses the three resolve models and five `USER_LIVE_EVENT_TYPES`, gains `UsageReportRequest`/`UsageReportResponse`/`handle_usage_report`.
- `core/repositories/messaging_repo.py` — `claim_usage_receipt`, `has_open_runs`.
- `routers/messaging.py` — usage route; `chat_gate` for steer.
- Call sites: `routers/admin.py`, `routers/channels.py`, `routers/container_recover.py`, `routers/websocket_chat.py`, `routers/teams/agents.py`, `core/services/{browser_state,desktop_pair_live,spend_service,node_proxy,admin_service,paperclip_agent_lifecycle,container_auth,paperclip_openclaw_pairing,catalog_service,company_deploy_service,openclaw_work,operator_proxy}.py`.
- `core/services/readiness_state.py`, `routers/container.py`, `core/services/connection_service.py`, `core/services/durable_activity.py`, `core/services/update_service.py`, `main.py`, `core/containers/__init__.py`, `core/gateway/operator_rpc.py`.
- Deleted: `core/gateway/connection_pool.py`, `core/services/cron_run_telemetry.py`, and the tests listed in Task 9.

**Clients** — `packages/shared/src/live/types.ts`, `src/runtime/{client,types}.ts`, `src/index.ts`, `src/runtime/client.test.ts`.

**Infra / docs** — `apps/infra/lib/stacks/observability-stack.ts` + `test/observability-stack.test.ts`, `openclaw-version.json`, `.github/workflows/deploy.yml` (regenerated), `CLAUDE.md`.

---

### Task 1: Plugin — manifest contract and the RPC route

**Files:**
- Modify: `apps/infra/openclaw/plugins/lightbulb-channel/openclaw.plugin.json`
- Create: `apps/infra/openclaw/plugins/lightbulb-channel/src/rpc.ts`
- Modify: `apps/infra/openclaw/plugins/lightbulb-channel/src/index.ts:190-203`
- Test: `apps/infra/openclaw/plugins/lightbulb-channel/src/rpc.test.ts`

**Interfaces:**
- Consumes: `dispatchGatewayMethod(method, params?, {timeoutMs?}) -> Promise<{ok, payload?, error?: {code, message}}>` from `openclaw/plugin-sdk/gateway-method-runtime`; `readJsonWebhookBodyOrReject({req, res, maxBytes, timeoutMs, invalidJsonMessage}) -> {ok, value}` from `openclaw/plugin-sdk/webhook-ingress`; `api.registerHttpRoute(params)` and `api.on(hook, handler)` on the `registerFull` api.
- Produces: `RPC_ROUTE = "/plugins/lightbulb-channel/rpc"`, `createRpcHandler(deps)`; the wire contract in Global Constraints (Task 3's backend client depends on it).

- [ ] **Step 1: Write the failing tests**

```ts
// src/rpc.test.ts
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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- rpc`
Expected: FAIL (module `./rpc.js` not found).

- [ ] **Step 3: Implement**

`openclaw.plugin.json` — add after `"activation"`:

```json
  "contracts": {
    "gatewayMethodDispatch": ["authenticated-request"]
  },
```

`src/rpc.ts`:

```ts
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
```

`src/index.ts` — imports and `registerFull`:

```ts
import { dispatchGatewayMethod } from "openclaw/plugin-sdk/gateway-method-runtime";
import { RPC_ROUTE, createRpcHandler } from "./rpc.js";
// (readJsonWebhookBodyOrReject is already imported from "openclaw/plugin-sdk/webhook-ingress")
```

```ts
  registerFull(api) {
    api.registerTool((ctx) => localSessionTool(ctx, {
      env: () => process.env,
      fetch: (url, init) => fetch(url, init),
      nodes: () => api.runtime.nodes.list({ connected: true }),
    }), { names: ["lightbulb_local_sessions"] });
    // Registered here, not through registerPluginHttpRoute: only the plugin-API path reads the
    // manifest's gatewayMethodDispatch contract and grants dispatchGatewayMethod to the route.
    api.registerHttpRoute({
      path: RPC_ROUTE,
      auth: "gateway",
      match: "exact",
      gatewayRuntimeScopeSurface: "trusted-operator",
      handler: createRpcHandler({ dispatch: dispatchGatewayMethod, readBody: readJsonWebhookBodyOrReject as any }),
    });
  },
```

If `api.registerHttpRoute`'s params type rejects `handler`'s return type or `gatewayRuntimeScopeSurface`, read `node_modules/openclaw/dist/plugin-entry-*.d.ts` `OpenClawPluginHttpRouteParams` and match it exactly; do not cast the whole object.

- [ ] **Step 4: Run the tests and the build**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- rpc && npm run build`
Expected: PASS; `dist/rpc.js` exists.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/openclaw/plugins/lightbulb-channel/openclaw.plugin.json apps/infra/openclaw/plugins/lightbulb-channel/src/rpc.ts apps/infra/openclaw/plugins/lightbulb-channel/src/rpc.test.ts apps/infra/openclaw/plugins/lightbulb-channel/src/index.ts apps/infra/openclaw/plugins/lightbulb-channel/dist
git commit -m "feat(plugin): gateway RPC over HTTP with in-process dispatch"
```

---

### Task 2: Plugin — `agent_end` usage hook

**Files:**
- Modify: `apps/infra/openclaw/plugins/lightbulb-channel/src/runs.ts:100-140` (`createRunCallbacks`), `:194-205` (`usageFor`)
- Create: `apps/infra/openclaw/plugins/lightbulb-channel/src/usage.ts`
- Modify: `apps/infra/openclaw/plugins/lightbulb-channel/src/index.ts` (`registerFull`)
- Test: `apps/infra/openclaw/plugins/lightbulb-channel/src/usage.test.ts`, `src/runs.test.ts`

**Interfaces:**
- Consumes: hook `agent_end` `(event: {runId?, success, error?, durationMs?}, ctx: {agentId?, sessionKey?})`; `getSessionEntry` as wired in `index.ts` (`SessionUsageEntry`).
- Produces: `postToBackend(deps, path, body): Promise<void>` and `usageFromEntry(entry): RunUsage` exported from `runs.ts`; `createUsageHook(deps)`; the usage callback body Task 7 parses.

- [ ] **Step 1: Write the failing tests**

```ts
// src/usage.test.ts
import { describe, expect, it, vi } from "vitest";
import { createUsageHook } from "./usage.js";

const ENTRY = { model: "claude-sonnet-4-6", inputTokens: 12, outputTokens: 3, cacheRead: 50, cacheWrite: 1, estimatedCostUsd: 0.01 };
const CTX = { agentId: "friday", sessionKey: "agent:friday:cron:job-1" };

describe("createUsageHook", () => {
  it("posts usage for a non-room run", async () => {
    const post = vi.fn(async () => {});
    await createUsageHook({ post, getSessionEntry: () => ENTRY })({ runId: "run-1", success: true, durationMs: 1500 }, CTX);
    expect(post).toHaveBeenCalledWith("/api/v1/internal/messaging/usage", {
      sessionKey: CTX.sessionKey, agentId: "friday", runId: "run-1", success: true, durationMs: 1500,
      usage: { model: "claude-sonnet-4-6", inputTokens: 12, outputTokens: 3, cacheReadTokens: 50, cacheWriteTokens: 1, estimatedCostUsd: 0.01 },
    });
  });

  it("skips room runs and runs without ids", async () => {
    const post = vi.fn(async () => {});
    const hook = createUsageHook({ post, getSessionEntry: () => ENTRY });
    await hook({ runId: "r", success: true }, { agentId: "friday", sessionKey: "agent:friday:lightbulb:channel:22222222-2222-4222-8222-222222222222" });
    await hook({ success: true }, CTX);
    await hook({ runId: "r", success: true }, { sessionKey: CTX.sessionKey });
    expect(post).not.toHaveBeenCalled();
  });

  it("never rejects and never forwards the error text", async () => {
    const post = vi.fn(async () => { throw new Error("down"); });
    const log = vi.fn();
    await expect(createUsageHook({ post, getSessionEntry: () => undefined, log })({ runId: "r", success: false, error: "secret text" }, CTX)).resolves.toBeUndefined();
    expect(JSON.stringify(post.mock.calls)).not.toContain("secret text");
    expect(log).toHaveBeenCalledTimes(1);
  });
});
```

Add to `src/runs.test.ts` (inside the existing `describe("createRunCallbacks")` or a new one):

```ts
import { postToBackend, usageFromEntry } from "./runs.js";

it("postToBackend retries 5xx and stops on 2xx/4xx", async () => {
  vi.useFakeTimers();
  try {
    const fetch = vi.fn().mockResolvedValueOnce({ status: 503 }).mockResolvedValueOnce({ status: 404 });
    const p = postToBackend({ fetch: fetch as any, env: () => ENV }, "/api/v1/internal/messaging/usage", { a: 1 });
    await vi.runAllTimersAsync();
    await p;
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[0][0]).toBe("https://api.flickmeon.ai/api/v1/internal/messaging/usage/owner_123");
  } finally {
    vi.useRealTimers();
  }
});

it("usageFromEntry maps a session entry and tolerates an empty one", () => {
  expect(usageFromEntry(ENTRY)).toEqual({ model: "claude-sonnet-4-6", inputTokens: 1200, outputTokens: 300, cacheReadTokens: 5000, cacheWriteTokens: 100, estimatedCostUsd: 0.0123 });
  expect(usageFromEntry(undefined)).toEqual({ model: "unknown", inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, estimatedCostUsd: null });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test -- usage runs`
Expected: FAIL.

- [ ] **Step 3: Implement**

`src/runs.ts` — extract the poster and the usage mapper; `createRunCallbacks.send` and `usageFor` call them (behaviour unchanged, including the `/runs/{ownerId}/events` URL and the undelivered log line the existing tests assert):

```ts
/** POST one JSON body to the backend with A's retry shape: three attempts, 5xx retried, never rejects. */
export async function postToBackend(
  deps: { fetch: FetchLike; env: () => RawEnv },
  url: (base: string, ownerId: string) => string,
  body: unknown,
): Promise<boolean> {
  for (let attempt = 0; attempt < CALLBACK_ATTEMPTS; attempt++) {
    try {
      const resolved = resolveEnv(deps.env());
      const res = await deps.fetch(url(resolved.base, encodeURIComponent(resolved.ownerId)), {
        method: "POST",
        headers: { "content-type": "application/json", Authorization: `Bearer ${resolved.token}` },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(CALLBACK_TIMEOUT_MS),
      });
      if (res.status < 500) return true; // 2xx applied/replayed and 4xx are both final
    } catch {
      // configuration or network failure: retry below
    }
    if (attempt + 1 < CALLBACK_ATTEMPTS) await new Promise((resolve) => setTimeout(resolve, 500 * (attempt + 1)));
  }
  return false;
}

export function createRunCallbacks(deps: { fetch: FetchLike; env: () => RawEnv; log?: (message: string) => void }): RunCallbacks {
  return {
    async send(event) {
      const delivered = await postToBackend(deps, (base, owner) => `${base}/api/v1/internal/messaging/runs/${owner}/events`, event);
      if (!delivered) deps.log?.(`lightbulb: run callback ${event.type} undelivered for ${event.runId}`);
    },
  };
}
```

The `runs.test.ts` addition then reads `postToBackend({ fetch: fetch as any, env: () => ENV }, (base, owner) => \`${base}/api/v1/internal/messaging/usage/${owner}\`, { a: 1 })` and asserts the first fetch URL is `https://api.flickmeon.ai/api/v1/internal/messaging/usage/owner_123` and the resolved value is `true`.

```ts
export function usageFromEntry(entry: SessionUsageEntry | undefined): RunUsage {
  const e = entry ?? {};
  const cost = e.estimatedCostUsd;
  return {
    model: typeof e.model === "string" && e.model ? e.model : "unknown",
    inputTokens: asCount(e.inputTokens),
    outputTokens: asCount(e.outputTokens),
    cacheReadTokens: asCount(e.cacheRead),
    cacheWriteTokens: asCount(e.cacheWrite),
    estimatedCostUsd: typeof cost === "number" && Number.isFinite(cost) && cost > 0 ? cost : null,
  };
}
// inside createRunSupervisor:
function usageFor(run: ActiveRun): RunUsage {
  return usageFromEntry(deps.sdk.getSessionEntry({ agentId: run.agentId, sessionKey: run.sessionKey }));
}
```

`src/usage.ts`:

```ts
import { usageFromEntry, type SessionUsageEntry } from "./runs.js";

const ROOM_SESSION_RE = /^agent:[^:]+:lightbulb:channel:[0-9a-f-]{36}$/;

/** `agent_end` for every run the room contract does not cover (cron, heartbeat, Paperclip). */
export function createUsageHook(deps: {
  post: (path: string, body: unknown) => Promise<void>;
  getSessionEntry: (params: { agentId: string; sessionKey: string }) => SessionUsageEntry | undefined;
  log?: (message: string) => void;
}) {
  return async (
    event: { runId?: string; success: boolean; error?: string; durationMs?: number },
    ctx: { agentId?: string; sessionKey?: string },
  ): Promise<void> => {
    const { agentId, sessionKey } = ctx;
    if (!event.runId || !agentId || !sessionKey || ROOM_SESSION_RE.test(sessionKey)) return;
    try {
      const usage = usageFromEntry(deps.getSessionEntry({ agentId, sessionKey }));
      // `event.error` is deliberately not forwarded: it can carry model output.
      await deps.post("/api/v1/internal/messaging/usage", {
        sessionKey, agentId, runId: event.runId, success: event.success,
        ...(typeof event.durationMs === "number" ? { durationMs: event.durationMs } : {}),
        usage,
      });
    } catch (error) {
      deps.log?.(`lightbulb: usage hook failed (${error instanceof Error ? error.name : "error"})`);
    }
  };
}
```

`src/index.ts` — in `registerFull`, after the route:

```ts
    api.on("agent_end", createUsageHook({
      post: async (path, body) => {
        const delivered = await postToBackend({ fetch: fetchLike, env: () => process.env }, (base, owner) => `${base}${path}/${owner}`, body);
        if (!delivered) console.warn(`lightbulb: usage callback undelivered`);
      },
      getSessionEntry: ({ agentId, sessionKey }) =>
        getSessionEntry({ agentId, sessionKey, env: process.env, readConsistency: "latest" }) as any,
      log: (m) => console.warn(m),
    }));
```

(import `postToBackend` from `./runs.js` and `createUsageHook` from `./usage.js`; `post`'s `(path, body)` shape is what `usage.test.ts` asserts.)

- [ ] **Step 4: Run all plugin tests and build**

Run: `cd apps/infra/openclaw/plugins/lightbulb-channel && npm test && npm run build`
Expected: PASS; `dist/usage.js` exists.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/openclaw/plugins/lightbulb-channel/src apps/infra/openclaw/plugins/lightbulb-channel/dist
git commit -m "feat(plugin): report non-room run usage from the agent_end hook"
```

Tasks 1–2 are the plugin PR: open it against `main`, wait for the required check, merge; `build-openclaw-image.yml` builds `lightbulb/openclaw-extended:2026.9.3-isol8.2-<sha7>`. Record the tag and digest in the ledger for Task 10.

---

### Task 3: Backend — `container_rpc`

**Files:**
- Create: `apps/backend/core/services/container_rpc.py`
- Modify: `apps/backend/core/services/runtime_rpc.py:246-270` (`_running_container` moves; keep a one-line import alias `from core.services.container_rpc import running_container as _running_container` so this task changes nothing else in the file)
- Test: `apps/backend/tests/unit/services/test_container_rpc.py`

**Interfaces:**
- Consumes: `get_ecs_manager().resolve_running_container(owner_id) -> (container | None, ip | None)`; `container_repo.get_by_owner_id`; `container_wake.request_wake_silent`; `ApiProblem` from `core.services.messaging`; `put_metric`.
- Produces: `running_container(owner_id) -> tuple[dict, str]`, `call(owner_id, method, params=None, *, timeout_s=30.0) -> Any`, `GATEWAY_PORT = 18789`, `RPC_PATH = "/plugins/lightbulb-channel/rpc"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_container_rpc.py
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services import container_rpc
from core.services.messaging import ApiProblem

CONTAINER = {"gateway_token": "gw", "status": "running"}


def _client(handler):
    transport = httpx.MockTransport(handler)
    return lambda **kw: httpx.AsyncClient(transport=transport, **kw)


async def _call(handler, **kw):
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=(CONTAINER, "10.0.0.9"))
    with (
        patch("core.services.container_rpc.get_ecs_manager", return_value=manager),
        patch("core.services.container_rpc.httpx.AsyncClient", _client(handler)),
    ):
        return await container_rpc.call("owner_1", "agents.list", {"a": 1}, **kw)


@pytest.mark.asyncio
async def test_ok_returns_the_payload_and_posts_the_contract():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "payload": {"agents": []}})

    assert await _call(handler, timeout_s=7) == {"agents": []}
    assert seen == {
        "url": "http://10.0.0.9:18789/plugins/lightbulb-channel/rpc",
        "auth": "Bearer gw",
        "body": {"method": "agents.list", "params": {"a": 1}, "timeoutMs": 7000},
    }


@pytest.mark.asyncio
async def test_gateway_error_is_a_runtime_error_with_the_error_json():
    error = {"code": "NOT_FOUND", "message": "no such agent"}
    with pytest.raises(RuntimeError) as exc:
        await _call(lambda r: httpx.Response(200, json={"ok": False, "error": error}))
    assert json.loads(str(exc.value)) == error


@pytest.mark.asyncio
async def test_504_is_a_timeout_error():
    with pytest.raises(asyncio.TimeoutError):
        await _call(lambda r: httpx.Response(504, json={"ok": False, "error": {"code": "AGENT_TIMEOUT", "message": "slow"}}))


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), lambda r: httpx.Response(502, text="bad gateway")])
async def test_transport_and_non_contract_answers_are_runtime_unavailable(handler):
    with pytest.raises(ApiProblem) as exc:
        await _call(handler)
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    assert exc.value.error.retryable is True


@pytest.mark.asyncio
async def test_no_running_container_is_runtime_unavailable_before_any_http():
    manager = MagicMock()
    manager.resolve_running_container = AsyncMock(return_value=(None, None))
    with (
        patch("core.services.container_rpc.get_ecs_manager", return_value=manager),
        patch("core.services.container_rpc.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value={"status": "stopped"}),
        patch("core.services.container_wake.request_wake_silent", new_callable=AsyncMock) as wake,
        pytest.raises(ApiProblem) as exc,
    ):
        await container_rpc.call("owner_1", "health")
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
    wake.assert_awaited_once_with("owner_1")
```

If `ApiProblem` exposes its code differently than `exc.value.error.code`, read `core/services/messaging.py` `class ApiProblem` and use its attribute (Task 13 of A's plan used `exc.value.error.code`).

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_container_rpc.py -q --no-cov`
Expected: FAIL (no module `container_rpc`).

- [ ] **Step 3: Implement**

```python
# core/services/container_rpc.py
"""One HTTP request per gateway RPC, to the container's baked lightbulb-channel plugin.

Replaces the per-owner gateway WebSocket pool. ``call`` raises exactly what the pool's
``send_rpc`` raised so every caller's ``except`` chain is unchanged.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from core.containers import get_ecs_manager
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services.messaging import ApiProblem

GATEWAY_PORT = 18789
RPC_PATH = "/plugins/lightbulb-channel/rpc"


async def running_container(owner_id: str) -> tuple[dict, str]:
    """The owner's running container row and IP; wakes a stopped one and reports retryable."""
    manager = get_ecs_manager()
    container, ip = await manager.resolve_running_container(owner_id)
    if container and ip:
        token = container.get("gateway_token")
        if isinstance(token, str) and token:
            return container, ip
    row = container or await container_repo.get_by_owner_id(owner_id, consistent=True)
    if row and row.get("status") == "stopped":
        from core.services.container_wake import request_wake_silent

        await request_wake_silent(owner_id)
        raise ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is waking. Try again shortly.", retryable=True, retry_after=3)
    raise ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is temporarily unavailable.", retryable=True, retry_after=3)


def _unavailable() -> ApiProblem:
    return ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is temporarily unavailable.", retryable=True, retry_after=3)


async def call(owner_id: str, method: str, params: dict | None = None, *, timeout_s: float = 30.0) -> Any:
    container, ip = await running_container(owner_id)
    started = time.perf_counter()
    outcome = "unavailable"
    try:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s + 5.0, connect=5.0)) as client:
                response = await client.post(
                    f"http://{ip}:{GATEWAY_PORT}{RPC_PATH}",
                    json={"method": method, "params": params or {}, "timeoutMs": int(timeout_s * 1000)},
                    headers={"Authorization": f"Bearer {container['gateway_token']}"},
                )
        except httpx.HTTPError as exc:
            raise _unavailable() from exc
        if response.status_code == 504:
            outcome = "timeout"
            raise asyncio.TimeoutError(f"{method} timed out")
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code != 200 or not isinstance(body, dict):
            raise _unavailable()
        if body.get("ok") is True:
            outcome = "ok"
            return body.get("payload")
        outcome = "error"
        raise RuntimeError(json.dumps(body.get("error") or {"code": "UNAVAILABLE", "message": "gateway method failed"}))
    finally:
        put_metric("messaging.rpc", dimensions={"outcome": outcome, "method": method})
        put_metric("messaging.rpc.latency", value=(time.perf_counter() - started) * 1000, unit="Milliseconds", dimensions={"method": method})
```

`runtime_rpc.py`: delete `_running_container` (lines 246–270) and add `from core.services.container_rpc import running_container as _running_container`. Check the `put_metric` signature in `core/observability/metrics.py:88` accepts `value=`/`unit=` (A's consumer uses exactly this form).

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_container_rpc.py tests/unit/services/test_runtime_rpc_guard.py -q --no-cov && uv run ruff check core/services/container_rpc.py core/services/runtime_rpc.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/container_rpc.py apps/backend/core/services/runtime_rpc.py apps/backend/tests/unit/services/test_container_rpc.py
git commit -m "feat(backend): container_rpc — gateway RPC over the plugin HTTP route"
```

---

### Task 4: Backend — `session_keys` and `chat_gate` out of the pool

**Files:**
- Create: `apps/backend/core/services/session_keys.py`, `apps/backend/core/services/chat_gate.py`
- Modify: `apps/backend/core/gateway/connection_pool.py` (delete `_parse_session_key`, `parse_lightbulb_session_key`, `gate_chat`; import the two parsers from `session_keys` where the pool still uses them), `core/services/operator_proxy.py:99`, `routers/messaging.py` (the `parse_lightbulb_session_key` import and the steer route's `gate_chat`), `routers/websocket_chat.py:13,234`, `core/services/messaging.py` (if it imports `parse_lightbulb_session_key` from the pool — grep)
- Move: `tests/unit/gateway/test_session_key_parser.py` → `tests/unit/services/test_session_keys.py`; `tests/unit/gateway/test_gate_chat_trial_cap.py` → `tests/unit/services/test_chat_gate.py`

**Interfaces:**
- Produces: `session_keys.parse_session_key(session_key) -> dict` (body of `_parse_session_key`, verbatim), `session_keys.parse_lightbulb_session_key(session_key) -> dict | None` (verbatim), `chat_gate.gate_chat(*, user_id, owner_id, session_key) -> dict` (body of the pool method verbatim, `self` removed).

- [ ] **Step 1: Move the tests**

`git mv` both files; change their imports to `from core.services.session_keys import parse_session_key` / `from core.services.chat_gate import gate_chat` and their call sites (`gate_chat(...)` instead of `pool.gate_chat(...)`; a fixture that built a pool becomes unnecessary — delete it and call the function). Rename `_parse_session_key` → `parse_session_key` inside the test.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_keys.py tests/unit/services/test_chat_gate.py -q --no-cov`
Expected: FAIL (modules missing).

- [ ] **Step 3: Implement**

Create the two modules by cutting the bodies from `connection_pool.py` (no edits to the logic; the pool's `_parse_session_key` becomes `parse_session_key`; `gate_chat` drops `self` and any `self.` reference — there are none, it reads `billing_repo` and the trial predicate only). Update every importer:

```bash
cd apps/backend && git grep -n "_parse_session_key\|parse_lightbulb_session_key\|gate_chat" -- core routers
```

Every hit outside `connection_pool.py` moves to the new modules; hits inside the pool import from `core.services.session_keys` (the pool is deleted in Task 9, so keep this minimal).

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_keys.py tests/unit/services/test_chat_gate.py tests/unit/routers/test_messaging_run_controls.py tests/unit/routers/test_messaging_run_status.py -q --no-cov && uv run python -c "import core.gateway.connection_pool, routers.messaging, routers.websocket_chat, core.services.operator_proxy"`
Expected: PASS. In `test_messaging_run_controls.py` the `pool.gate_chat` patch becomes `patch("routers.messaging.chat_gate.gate_chat", new_callable=AsyncMock, return_value={"blocked": False})` (adjust to how the router imports it).

- [ ] **Step 5: Commit**

```bash
git add -A apps/backend/core apps/backend/routers apps/backend/tests
git commit -m "refactor(backend): session key parsers and the chat gate leave the gateway pool"
```

---

### Task 5: Backend — `runtime_rpc` on `container_rpc`; invalidation; the approval/question rail deleted

**Files:**
- Modify: `apps/backend/core/services/runtime_rpc.py` (`execute_sync` :302-368, `_create_agent` :387-430, `execute_control` :521-557 deleted)
- Modify: `apps/backend/routers/runtime_rpc.py` (two routes deleted), `apps/backend/core/services/messaging.py:92-107` (`USER_LIVE_EVENT_TYPES`), `:474-493` (three models deleted)
- Test: `apps/backend/tests/unit/services/test_runtime_rpc_invalidation.py` (new), `tests/unit/services/test_runtime_rpc_agent_create.py`, `test_runtime_rpc_agent_update.py`, `test_runtime_rpc_seeded_delete.py`, `test_runtime_rpc_guard.py`, and any router test for the deleted routes (`git grep -ln "approvals/\|questions/" tests/unit/routers`)

**Interfaces:**
- Consumes: `container_rpc.call`; `publish_owner_users(owner_id, event)`; `make_live_event(type, payload)`.
- Produces: `_INVALIDATES` mapping and `_invalidate(owner_id, method)`; `execute_sync` semantics unchanged for callers (`openclaw_work`'s operation worker included).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/services/test_runtime_rpc_invalidation.py
from unittest.mock import AsyncMock, patch

import pytest

from core.services import runtime_rpc as rpc


async def _run(method, params=None, *, call=None):
    with (
        patch("core.services.runtime_rpc.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}) as rpc_call,
        patch("core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock) as publish,
    ):
        if call is not None:
            rpc_call.side_effect = call
        result = await rpc.execute_sync(owner_id="org_1", user_id="user_1", request_id="r", method=method, params=params or {})
    return result, rpc_call, publish


@pytest.mark.asyncio
async def test_mutating_config_call_publishes_invalidation():
    result, call, publish = await _run("config.patch", {"raw": "{}"})
    assert result == {"ok": True}
    call.assert_awaited_once_with("org_1", "config.patch", {"raw": "{}"}, timeout_s=rpc._RPC_TIMEOUT_S)
    event = publish.await_args.args[1]
    assert (publish.await_args.args[0], event["type"], event["payload"]) == ("org_1", "runtime.resource.invalidated", {"resource": "config"})


@pytest.mark.asyncio
async def test_read_call_publishes_nothing():
    _, _, publish = await _run("config.get")
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failure_does_not_fail_the_rpc():
    with patch("core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock, side_effect=RuntimeError("appsync")):
        with patch("core.services.runtime_rpc.container_rpc.call", new_callable=AsyncMock, return_value=1):
            assert await rpc.execute_sync(owner_id="org_1", user_id="user_1", request_id="r", method="skills.install", params={"name": "x"}) == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_runtime_rpc_invalidation.py -q --no-cov`
Expected: FAIL.

- [ ] **Step 3: Implement**

`runtime_rpc.py`:

```python
from core.services import container_rpc
from core.services.live_events import publish_owner_users
from core.services.messaging import make_live_event

_RPC_TIMEOUT_S = 30.0
_INVALIDATES = {
    "agents.update": "agents",
    "agents.delete": "agents",
    "config.set": "config",
    "config.patch": "config",
    "skills.install": "skills",
    "skills.update": "skills",
    "skills.proposals.apply": "skills",
    "skills.proposals.reject": "skills",
}


async def _invalidate(owner_id: str, method: str) -> None:
    """Replaces the gateway's `*.changed` broadcasts the pool used to relay. Never raises."""
    resource = _INVALIDATES.get(method)
    if resource is None:
        return
    try:
        await publish_owner_users(owner_id, make_live_event("runtime.resource.invalidated", {"resource": resource}))
    except Exception:
        logger.warning("resource invalidation publish failed resource=%s", resource, exc_info=True)
```

`execute_sync` — the `agents.delete` branch:

```python
        try:
            result = await delete_agent_everywhere(owner_id=owner_id, agent_id=agent_id)
        except AgentLifecycleConflict as exc:
            raise ApiProblem("AGENT_LIFECYCLE_CONFLICT", str(exc)) from exc
        await _invalidate(owner_id, method)
        return result
```

the `agents.create` branch and the RPC block:

```python
    if method == "agents.create":
        container, ip = await container_rpc.running_container(owner_id)
        return await _create_agent(owner_id, user_id, _sanitize_agent_create(params), ip, container["gateway_token"])
    if method == "agents.update":
        ...unchanged...
    try:
        result = await container_rpc.call(owner_id, method, params, timeout_s=_RPC_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except ApiProblem:
        raise
    except asyncio.TimeoutError as exc:
        ...unchanged...
    except RuntimeError as exc:
        raise _normalize_gateway_error(method, exc) from exc
    except Exception as exc:
        ...unchanged...
    await _invalidate(owner_id, method)
    if method == "sessions.list":
        ...unchanged...
```

Delete the `container, ip = await _running_container(owner_id)` / `token = container["gateway_token"]` lines that preceded the `agents.create` branch (only `agents.create` still needs them, for `ensure_operator_device_paired`). Leave `_create_agent` and the `get_gateway_pool` import alone in this task: Task 6 rewrites `_create_agent` together with `pair_with_openclaw`'s signature and removes the import then. Delete `execute_control` here.

`routers/runtime_rpc.py`: delete `resolve_approval`, `resolve_question` and the three model imports. `core/services/messaging.py`: delete `ResolveApprovalRequest`, `ResolveQuestionRequest`, `ResolveRuntimeResponse`, and remove `runtime.approval.requested`, `runtime.approval.resolved`, `runtime.question.requested`, `runtime.question.resolved`, `runtime.progress.changed` from `USER_LIVE_EVENT_TYPES`.

Tests: in `test_runtime_rpc_agent_create.py`, `..._agent_update.py`, `..._seeded_delete.py`, `..._guard.py` replace `pool.send_rpc = AsyncMock(...)` / `monkeypatch.setattr(rpc, "get_gateway_pool", lambda: pool)` with `monkeypatch.setattr(rpc.container_rpc, "call", AsyncMock(return_value=...))` and assert on that mock. Delete tests of the two routes and of `execute_control`.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_runtime_rpc_invalidation.py tests/unit/services/test_runtime_rpc_agent_create.py tests/unit/services/test_runtime_rpc_agent_update.py tests/unit/services/test_runtime_rpc_seeded_delete.py tests/unit/services/test_runtime_rpc_guard.py tests/unit/routers -q --no-cov && uv run ruff check core/services/runtime_rpc.py routers/runtime_rpc.py core/services/messaging.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A apps/backend
git commit -m "feat(backend): runtime RPC over container_rpc; delete the unconsumed approval/question rail"
```

---

### Task 6: Backend — every other pool and one-shot caller

**Files (all Modify):**
- `apps/backend/routers/admin.py:732-764`, `routers/channels.py:85-104`, `routers/container_recover.py:39-62,207,220,273-282`, `routers/websocket_chat.py` (already on `chat_gate` from Task 4 — verify), `routers/teams/agents.py:598-608`
- `core/services/browser_state.py:82-98,108-123,161-225`, `desktop_pair_live.py:92-105` + its `_pool_rpc` call sites `:177,232,284,340,349` + the `ip`/`gateway_token` params of `heal_desktop_device_not_paired` and `approve_pending_node_surface` and their callers `node_proxy.py:273-277,361-365`, `spend_service.py:218-243`, `node_proxy.py:443-460`, `admin_service.py:547-562`, `paperclip_agent_lifecycle.py:213-240,363-390`, `container_auth.py:33-47,62-86`, `paperclip_openclaw_pairing.py:498-527` (`_find_pending_request_id`), `:531-580` (`_approve_pairing_request`), `:601-680` (`approve_pending_device_from_rejection`), `:907-1015` (`pair_with_openclaw`), `catalog_service.py:28,380-393,634-641`, `company_deploy_service.py:49,288,336`, `openclaw_work.py:20,392-395`, `operator_proxy.py:343-349`
- Tests: every file under `tests/` that patches `get_gateway_pool`, `send_rpc` or `gateway_rpc_oneshot` for these modules (`git grep -ln "get_gateway_pool\|send_rpc\|gateway_rpc_oneshot\|gateway_pool" tests`), excluding the pool's own tests (Task 9 deletes those).

**Interfaces:**
- Consumes: `container_rpc.call`; `ApiProblem`.
- Produces: `pair_with_openclaw(*, owner_id, paperclip_agent, extra_update_fields=None)`; `_find_pending_request_id(*, owner_id, device_id, timeout_s=10.0)`; `_approve_pairing_request(*, owner_id, request_id, device_id, timeout_s=10.0)` (keep its existing extra params if any beyond these); `approve_pending_device_from_rejection(*, owner_id, device_id, request_id)`; `heal_desktop_device_not_paired(*, owner_id, device_id, request_id)`; `approve_pending_node_surface(*, owner_id, device_id)`; `_browser_request(*, owner_id, method, path, body=None)`; `restore_browser_state(owner_id)` (drop `ip`, `token`; update `routers/container.py:640-660`); `revoke_provider_auth_in_container` unchanged externally; `delete_runtime_agent_with_lock_retry(*, owner_id, agent_id)`; `container_recover._call_gateway_rpc(owner_id, method, params=None)`.

- [ ] **Step 1: Rewrite the callers' tests**

For each test file found by the grep, replace the pool stub with `patch("core.services.container_rpc.call", new_callable=AsyncMock, return_value=<what send_rpc returned>)` (or `monkeypatch.setattr(<module>.container_rpc, "call", ...)` when the module does `from core.services import container_rpc`), and assert `call.await_args == call("<owner>", "<method>", <params>[, timeout_s=...])`. Where a test asserted `req_id` uniqueness or `ip`/`token` plumbing, delete that assertion (there is no request id or address any more). `tests/unit/services/test_paperclip_openclaw_pairing.py:620-650`: `pair_with_openclaw(owner_id=..., paperclip_agent=...)` with `call` awaited twice (`agents.create` then `agents.update`). `tests/unit/services/test_desktop_pair_live.py:34`: patch `core.services.desktop_pair_live.container_rpc.call`. `tests/unit/services/test_company_deploy_service.py` and `test_catalog_service.py` / `test_marketplace_hire.py` / `tests/credentials/test_marketplace_requires_credentials.py`: drop `gateway_pool_factory=` / `gateway_pool=` arguments and the pool fakes; the pair function fake keeps only `owner_id`, `paperclip_agent`, `extra_update_fields`.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_paperclip_openclaw_pairing.py tests/unit/services/test_desktop_pair_live.py tests/unit/services/test_browser_state.py tests/unit/services/test_admin_service.py tests/unit/services/test_agent_lifecycle_delete.py tests/unit/services/test_company_deploy_service.py tests/unit/services/test_catalog_service.py tests/unit/services/test_node_proxy.py tests/unit/routers/test_admin_actions_writes.py tests/unit/routers/test_teams_agents_pairing.py -q --no-cov`
Expected: FAIL.

- [ ] **Step 3: Implement, file by file**

`routers/admin.py`:

```python
async def _admin_agent_rpc(*, user_id: str, agent_id: str, method: str) -> dict:
    """One gateway RPC for an admin-initiated per-agent action, keyed on the owner."""
    owner_id, _ = await resolve_admin_owner_id(user_id)
    try:
        return await container_rpc.call(owner_id, method, {"agent_id": agent_id})
    except ApiProblem as exc:
        raise HTTPException(status_code=409, detail="container_not_running") from exc
```

and drop the `req_prefix=` argument at its call site (`:798`).

`routers/channels.py:86-104` becomes:

```python
        status = await container_rpc.call(owner_id, "channels.status", {"probe": True})
```

(the surrounding `try/except Exception` at `:118` already treats any failure as the fallback; delete the local `get_ecs_manager, get_gateway_pool` import).

`routers/container_recover.py`:

```python
async def _call_gateway_rpc(owner_id: str, method: str, params: dict | None = None) -> dict:
    """Recovery keeps a tight budget: a wedged gateway must fail fast so the flow escalates."""
    return await container_rpc.call(owner_id, method, params, timeout_s=10.0)
```

call sites: `_call_gateway_rpc(owner_id, "health")`, `_call_gateway_rpc(owner_id, "update.run")`; the `config.apply` block: `await container_rpc.call(owner_id, "config.apply", {"raw": json.dumps(config, separators=(",", ":"))}, timeout_s=30.0)` with `except (ApiProblem, ConnectionRefusedError)` → 502 "Gateway is not responding", `except (TimeoutError, asyncio.TimeoutError)` → 502 "Gateway restart timed out" (keep the generic branch). Remove the `gateway_rpc_oneshot` import.

`core/services/browser_state.py`:

```python
async def _browser_request(*, owner_id: str, method: str, path: str, body: dict | None = None):
    params: dict = {"method": method, "path": path}
    if body is not None:
        params["body"] = body
    return await container_rpc.call(owner_id, "browser.request", params, timeout_s=_RPC_TIMEOUT_S)
```

Snapshot path (`:108-123`): keep the `resolve_running_container` pre-check only if the function returns `False` for a cold container before doing EFS work — it does, so replace it with `try: await container_rpc.running_container(owner_id) except ApiProblem: return False`, drop `token`/`pool`, and call `_browser_request(owner_id=owner_id, method="GET", path=path)`. `restore_browser_state(owner_id)` drops `ip`, `token`, `pool`; `routers/container.py:640-660` `_browser_state_restore(owner_id)` likewise and its dispatch site passes only `owner_id`.

`core/services/desktop_pair_live.py`:

```python
async def _pool_rpc(*, owner_id: str, method: str, params: dict) -> dict:
    """One admin-scoped RPC to the owner's container."""
    payload = await container_rpc.call(owner_id, method, params, timeout_s=_RPC_TIMEOUT_S)
    return payload if isinstance(payload, dict) else {}
```

Every `_pool_rpc(...)` call drops `ip=`/`token=`; `heal_desktop_device_not_paired` and `approve_pending_node_surface` drop `ip`/`gateway_token` from their signatures and from their two callers in `node_proxy.py`; `push_desktop_pairing_live`/`push_desktop_revoke_live` keep their own `resolve_running_container` early-return (cold container → `False`) but pass only `owner_id` on.

`core/services/spend_service.py:218-243`:

```python
        result = await container_rpc.call(owner_id, "models.authStatus", {}, timeout_s=5.0)
```

(delete the `resolve_running_container` pre-check and the `asyncio.wait_for` wrapper; the existing `except Exception` → `None` covers `ApiProblem`; delete `_gateway_pool`).

`core/services/node_proxy.py:443-460`:

```python
                try:
                    await container_rpc.call(owner_id, "sessions.patch", {"key": sk, "execNode": None, "execHost": None})
                except Exception:
                    ...unchanged debug log...
```

`core/services/admin_service.py:547-562`: delete `pool = get_gateway_pool()` and `token = ...`; `_rpc(method, params)` → `return await container_rpc.call(owner_id, method, params)`; drop the `suffix` argument at each `_rpc(...)` call. Keep the `resolve_running_container` pre-check that returns `{"error": "container_unreachable"}`.

`core/services/paperclip_agent_lifecycle.py`: `delete_runtime_agent_with_lock_retry(*, owner_id, agent_id)` with `await container_rpc.call(owner_id, "agents.delete", {"agentId": agent_id, "deleteFiles": True})` in the retry loop; at `:363-390` drop `gateway = get_gateway_pool()`, use `container_rpc.call(owner_id, "agents.list", {})` under the same `if container is not None and ip:` guard (keep the guard: a cold container skips the live delete deliberately), and pass only `owner_id`/`agent_id` to the retry helper.

`core/services/container_auth.py`: `_logout_provider_via_gateway(owner_id, provider)` → `await container_rpc.call(owner_id, "models.authLogout", {"provider": provider}, timeout_s=_WS_TIMEOUT)`; `revoke_provider_auth_in_container` keeps its `resolve_running_container` cold-container early return and its `enc_seed` check, then calls `_logout_provider_via_gateway(owner_id, provider)`.

`core/services/paperclip_openclaw_pairing.py`:

```python
async def _find_pending_request_id(*, owner_id: str, device_id: str, timeout_s: float = 10.0) -> str | None:
    payload = await container_rpc.call(owner_id, "device.pair.list", {}, timeout_s=timeout_s)
    ...loop unchanged...
```

`_approve_pairing_request(*, owner_id, request_id, device_id, timeout_s=10.0)`: `await container_rpc.call(owner_id, "device.pair.approve", {"requestId": request_id}, timeout_s=timeout_s)` with its one re-list retry calling `_find_pending_request_id(owner_id=owner_id, device_id=device_id, timeout_s=timeout_s)`. `approve_pending_device_from_rejection(*, owner_id, device_id, request_id)` drops `ip`/`gateway_token` and passes `timeout_s=5.0` where it passed the three 5 s timeouts; update its two callers (`desktop_pair_live.py:141`, `operator_proxy.py:343`). `ensure_operator_device_paired` keeps `gateway_token`/`ip` (the signed-connect probe needs them) and calls the new signatures at `:821-830`. `pair_with_openclaw(*, owner_id, paperclip_agent, extra_update_fields=None)`: both `gateway_pool.send_rpc(...)` calls become `await container_rpc.call(owner_id, "agents.create", {...same params...})` / `"agents.update"`; the `except RuntimeError` branches are unchanged. Remove the `gateway_rpc_oneshot` import (keep `perform_signed_connect`).

`core/services/catalog_service.py`: delete the `gateway_pool_factory` constructor parameter, `_gateway_pool_factory`, `_get_gateway_pool`, and the `gateway_pool=`/`ip=`/`token=` arguments at `:634-641` (`container_ip`/`container_token` stay if anything else uses them; otherwise delete their resolution too). `core/services/company_deploy_service.py`: delete `get_gateway_pool` from the import, `gateway_pool = get_gateway_pool()`, and the `gateway_pool=` argument at `:336` (plus `ip=`/`token=` if passed there). `routers/teams/agents.py:598-608`: delete `gateway_pool = get_gateway_pool()` and the three arguments; keep `ensure_operator_device_paired(..., gateway_token=token, ip=ip)`.

`core/services/openclaw_work.py:392-395`: delete the three lines (`pool = get_gateway_pool()`, the comment, `await pool.ensure_connection(...)`) and `get_gateway_pool` from the import at `:20`; `token` is still used by `self._post`.

`core/services/runtime_rpc.py` `_create_agent`: `await pair_with_openclaw(owner_id=owner_id, paperclip_agent=paperclip_agent)`; delete the remaining `get_gateway_pool` import.

- [ ] **Step 4: Run the tests and the import gate**

Run: `cd apps/backend && uv run pytest tests/unit/services tests/unit/routers -q --no-cov -x --deselect tests/unit/services/test_local_session_relay.py && uv run ruff check core routers && git grep -n "send_rpc\|gateway_rpc_oneshot\|get_gateway_pool" -- core routers main.py`
Expected: PASS (report pre-existing reds on `main`, do not fix them); the grep hits only `core/gateway/connection_pool.py`, `core/gateway/operator_rpc.py`, `core/containers/__init__.py`, `main.py`, `core/services/durable_activity.py`, `routers/container.py` (Tasks 8–9 remove those).

- [ ] **Step 5: Commit**

```bash
git add -A apps/backend
git commit -m "refactor(backend): every gateway RPC goes through container_rpc"
```

---

### Task 7: Backend — usage route, receipt, `has_open_runs`; cron telemetry deleted

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py` (after `get_run`), `apps/backend/core/services/messaging.py` (after `RunEventResponse`; after `handle_run_event`), `apps/backend/routers/messaging.py` (after `post_run_event`), `apps/backend/core/services/update_service.py:60-63,1034,1085-1095`
- Delete: `apps/backend/core/services/cron_run_telemetry.py`, `apps/backend/tests/unit/services/test_cron_run_telemetry.py`
- Test: `apps/backend/tests/unit/services/test_usage_report.py` (new), `apps/backend/tests/unit/repositories/test_messaging_repo_usage.py` (new)

**Interfaces:**
- Consumes: `record_usage(owner_id, user_id, model, input_tokens, output_tokens, cache_read, cache_write, raw_cost_microcents, room_turn, event_kind)`; `run_analytics.schedule_run_completed(*, owner_id, parsed, payload, phase)`; `session_keys.parse_session_key`, `parse_lightbulb_session_key`; `RunUsage`; `_require_hook_token`.
- Produces: `messaging_repo.claim_usage_receipt(owner_id, run_id) -> bool`, `UsageReportRequest`, `UsageReportResponse`, `handle_usage_report(owner_id, body)`, route `POST /internal/messaging/usage/{owner_id}`. (`messaging_repo.has_open_runs` already exists — A's final fix round added it with its test; do not re-add it.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_usage_report.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import messaging
from core.services.messaging import RunUsage, UsageReportRequest

USAGE = RunUsage(model="claude-sonnet-4-6", input_tokens=10, output_tokens=2, cache_read_tokens=0, cache_write_tokens=0, estimated_cost_usd=0.01)
BODY = UsageReportRequest(session_key="agent:friday:cron:job-1", agent_id="friday", run_id="run-1", success=True, duration_ms=1200, usage=USAGE)


def _patches(claimed=True):
    return (
        patch("core.services.messaging.messaging_repo.claim_usage_receipt", new_callable=AsyncMock, return_value=claimed),
        patch("core.services.messaging.record_usage", new_callable=AsyncMock),
        patch("core.services.messaging.run_analytics.schedule_run_completed", MagicMock(return_value=None)),
    )


@pytest.mark.asyncio
async def test_first_report_records_usage_and_analytics():
    claim, record, analytics = _patches()
    with claim as claim, record as record, analytics as analytics:
        response = await messaging.handle_usage_report("org_1", BODY)
    assert response.status == "applied"
    claim.assert_awaited_once_with("org_1", "run-1")
    record.assert_awaited_once_with(owner_id="org_1", user_id="org_1", model="claude-sonnet-4-6", input_tokens=10, output_tokens=2, cache_read=0, cache_write=0, raw_cost_microcents=10_000, room_turn=False, event_kind=None)
    kwargs = analytics.call_args.kwargs
    assert (kwargs["owner_id"], kwargs["phase"], kwargs["payload"]["runId"], kwargs["parsed"]["source"]) == ("org_1", "end", "run-1", "cron")


@pytest.mark.asyncio
async def test_replay_and_room_sessions_write_nothing():
    claim, record, analytics = _patches(claimed=False)
    with claim, record as record, analytics as analytics:
        assert (await messaging.handle_usage_report("org_1", BODY)).status == "replayed"
        room = BODY.model_copy(update={"session_key": "agent:friday:lightbulb:channel:22222222-2222-4222-8222-222222222222"})
        assert (await messaging.handle_usage_report("org_1", room)).status == "ignored"
    record.assert_not_awaited()
    analytics.assert_not_called()


@pytest.mark.asyncio
async def test_zero_usage_skips_counters_but_still_emits_analytics():
    claim, record, analytics = _patches()
    empty = BODY.model_copy(update={"usage": RunUsage(model="unknown", input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0, estimated_cost_usd=None), "success": False})
    with claim, record as record, analytics as analytics:
        assert (await messaging.handle_usage_report("org_1", empty)).status == "applied"
    record.assert_not_awaited()
    assert analytics.call_args.kwargs["phase"] == "error"
```

```python
# tests/unit/repositories/test_messaging_repo_usage.py
from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError

from core.repositories import messaging_repo


def _conditional_failure(*args, **kwargs):
    raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")


@pytest.mark.asyncio
async def test_claim_usage_receipt_is_first_writer_wins():
    with patch("core.repositories.messaging_repo.run_in_thread", new_callable=AsyncMock) as run:
        assert await messaging_repo.claim_usage_receipt("org_1", "run-1") is True
        item = run.await_args.kwargs["Item"]
        assert (item["pk"], item["sk"]) == ("OWNER#org_1", "USAGE#run-1") and "expiresAt" in item
        run.side_effect = _conditional_failure
        assert await messaging_repo.claim_usage_receipt("org_1", "run-1") is False


```

Check `owner_pk("org_1")` really is `OWNER#org_1` (`messaging_repo.py:100`) and `run_in_thread` is the name the repo uses for its thread hop; adjust the patch targets if the module names differ.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_usage_report.py tests/unit/repositories/test_messaging_repo_usage.py -q --no-cov`
Expected: FAIL.

- [ ] **Step 3: Implement**

`messaging_repo.py`:

```python
def usage_sk(run_id: str) -> str:
    return f"USAGE#{run_id}"


async def claim_usage_receipt(owner_id: str, run_id: str) -> bool:
    """First writer wins: the usage callback is at-least-once, the counters are not idempotent."""
    item = {"pk": owner_pk(owner_id), "sk": usage_sk(run_id), "expiresAt": _expires_at()}
    try:
        await run_in_thread(_table().put_item, Item=item, ConditionExpression="attribute_not_exists(sk)")
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
```

`messaging.py` (imports: `from core.services import run_analytics`, `from core.services.session_keys import parse_session_key, parse_lightbulb_session_key`; `record_usage` is already imported for `_after_run_ended`):

```python
class UsageReportRequest(ContractModel):
    session_key: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    success: bool
    duration_ms: int | None = Field(default=None, ge=0)
    usage: RunUsage


class UsageReportResponse(ContractModel):
    run_id: str
    status: Literal["applied", "replayed", "ignored"]


async def handle_usage_report(owner_id: str, body: UsageReportRequest) -> UsageReportResponse:
    """Usage + product analytics for runs the room contract does not cover (cron, heartbeat, Paperclip)."""
    if parse_lightbulb_session_key(body.session_key) is not None:
        put_metric("messaging.usage.callback", dimensions={"outcome": "room"})
        return UsageReportResponse(run_id=body.run_id, status="ignored")
    if not await messaging_repo.claim_usage_receipt(owner_id, body.run_id):
        put_metric("messaging.usage.callback", dimensions={"outcome": "replayed"})
        return UsageReportResponse(run_id=body.run_id, status="replayed")
    usage = body.usage
    cost = usage.estimated_cost_usd
    raw_cost_microcents = round(cost * 1_000_000) if cost is not None and cost > 0 else None
    if usage.input_tokens > 0 or usage.output_tokens > 0 or raw_cost_microcents:
        try:
            await record_usage(
                owner_id=owner_id,
                user_id=owner_id,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read=usage.cache_read_tokens,
                cache_write=usage.cache_write_tokens,
                raw_cost_microcents=raw_cost_microcents,
                room_turn=False,
                event_kind=None,
            )
        except Exception:
            logger.exception("usage record failed")
    try:
        run_analytics.schedule_run_completed(
            owner_id=owner_id,
            parsed=parse_session_key(body.session_key),
            payload={"runId": body.run_id, "data": usage.model_dump(by_alias=True)},
            phase="end" if body.success else "error",
        )
    except Exception:
        logger.exception("run analytics scheduling failed")
    put_metric("messaging.usage.callback", dimensions={"outcome": "applied"})
    return UsageReportResponse(run_id=body.run_id, status="applied")
```

`routers/messaging.py` (next to `post_run_event`; add `UsageReportRequest`, `UsageReportResponse` to the import block):

```python
@router.post("/internal/messaging/usage/{owner_id}", response_model=UsageReportResponse)
async def post_usage_report(owner_id: str, body: UsageReportRequest, request: Request):
    await _require_hook_token(owner_id, request)
    return await service.handle_usage_report(owner_id, body)
```

`update_service.py`: delete `_CRON_RUNS_PASS_INTERVAL_SECONDS` and its comment (`:60-63`), `last_cron_runs_pass_at` (`:1034`) and the sweep block (`:1085-1095`); delete `core/services/cron_run_telemetry.py` and its test. Check `run_analytics.py:134` and `cron_run_telemetry`'s mention in `update_service.py:1027` docstring; fix the docstring, leave the `run_analytics` comment (it names a deleted caller: reword to "a later re-observation").

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_usage_report.py tests/unit/repositories/test_messaging_repo_usage.py tests/unit/services/test_update_service.py tests/unit/services/test_update_service_paperclip_passes.py tests/unit/services/test_update_service_graceful_stop.py tests/unit/routers/test_messaging_run_events.py -q --no-cov && uv run ruff check core routers && uv run python -c "import core.services.update_service, routers.messaging"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A apps/backend
git commit -m "feat(messaging): usage callback for non-room runs; delete the cron telemetry sweep"
```

---

### Task 8: Backend — readiness probe, self-heal and liveness rows deleted, trial idle on the ledger

**Files:**
- Modify: `apps/backend/core/services/readiness_state.py:176-180,229-232`, `apps/backend/routers/container.py:15-16,55-80,553-575,595-640,777-812`, `apps/backend/core/services/connection_service.py:35-63,333-580,598-606`, `apps/backend/core/services/durable_activity.py:9,36,48-58,74-80`
- Delete: `apps/backend/tests/unit/routers/test_container_status_selfheal_dial.py`, `apps/backend/tests/unit/services/test_connection_service_liveness.py`
- Test: `apps/backend/tests/unit/services/test_readiness_state.py`, `tests/unit/routers/test_container_status.py`, `tests/unit/routers/test_container_status_wake_gate.py` (patch target changes), `apps/backend/tests/unit/services/test_durable_activity.py` (exists since A's final fix round; the Step 1 snippet below is its target shape — reconcile rather than overwrite)

**Interfaces:**
- Consumes: `container_rpc.call`; `messaging_repo.has_open_runs`; `wake_policy.is_parked_trial` (unchanged).
- Produces: `readiness_state._gateway_reachable(owner_id) -> bool`; `sweep_trial_idle_once` without pool vetoes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/services/test_durable_activity.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import durable_activity

ROW = {"owner_id": "user_1", "status": "running", "last_interactive_at": 0}


def _patches(open_runs):
    manager = MagicMock()
    manager.stop_user_service = AsyncMock()
    connections = MagicMock()
    connections.has_live_operator_connection = AsyncMock(return_value=False)
    return manager, (
        patch("core.services.durable_activity.container_repo.get_by_status", new_callable=AsyncMock, return_value=[ROW]),
        patch("core.services.durable_activity.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=ROW),
        patch("core.services.durable_activity.container_repo.try_claim_idle_stop", new_callable=AsyncMock, return_value="claim"),
        patch("core.services.durable_activity.container_repo.idle_stop_claim_current", new_callable=AsyncMock, return_value=True),
        patch("core.services.durable_activity.billing_repo.get_by_owner_id", new_callable=AsyncMock, return_value={"subscription_status": "trial_local"}),
        patch("core.services.durable_activity.messaging_repo.has_open_runs", new_callable=AsyncMock, return_value=open_runs),
        patch("core.services.durable_activity.ConnectionService", return_value=connections),
        patch("core.services.durable_activity.get_ecs_manager", return_value=manager),
        patch("core.services.durable_activity.put_metric"),
    )


@pytest.mark.asyncio
async def test_open_ledger_run_vetoes_the_stop():
    manager, patches = _patches(open_runs=True)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        await durable_activity.sweep_trial_idle_once(now=10_000)
    manager.stop_user_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_owner_without_runs_is_stopped():
    manager, patches = _patches(open_runs=False)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
        await durable_activity.sweep_trial_idle_once(now=10_000)
    manager.stop_user_service.assert_awaited_once_with("user_1", reason="trial_idle")
```

(Use `contextlib.ExitStack` if the nine-way `with` reads badly.) In `test_readiness_state.py`, `test_container_status.py`, `test_container_status_wake_gate.py`: replace every `patch("core.services.connection_service.is_gateway_connected", ...)` with `patch("core.services.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True})` for "connected" and `side_effect=ApiProblem("RUNTIME_UNAVAILABLE", "x", retryable=True)` (or any `Exception`) for "not connected"; delete assertions about the self-heal dial.

- [ ] **Step 2: Run to verify failure**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_durable_activity.py tests/unit/services/test_readiness_state.py tests/unit/routers/test_container_status.py -q --no-cov`
Expected: FAIL.

- [ ] **Step 3: Implement**

`readiness_state.py`: delete the early `gateway_connected = ...` try/except (`:176-180`), add:

```python
async def _gateway_reachable(owner_id: str) -> bool:
    """One ``health`` RPC through the container plugin; any failure reads as not ready."""
    from core.services import container_rpc

    try:
        await container_rpc.call(owner_id, "health", timeout_s=2.0)
        return True
    except Exception:  # noqa: BLE001 — a probe must never break /status
        logger.info("readiness_state: gateway probe failed for owner %s", owner_id, exc_info=True)
        return False
```

and in the not-parked branch: `runtime_phase = resolve_runtime_phase(container, container.get("status") == "running" and await _gateway_reachable(owner_id))`. Update `resolve_runtime_phase`'s docstring line that names the connection-service table.

`routers/container.py`: delete `_self_heal_inflight`, `_self_heal_backoff_at`, `_self_heal_dial_allowed`, `_SELF_HEAL_BACKOFF_S`, the whole `if (container_ip and ... _self_heal_dial_allowed(owner_id)):` block with its comment, `_pool_self_heal_connect`, `_safe_phase`, `_resolve_cold_start_phase`, and the `GatewayReconnectThrottled` / `get_gateway_pool` imports. Keep `_browser_restore_inflight` and the browser-restore dispatch.

`connection_service.py`: delete `_LIVENESS_STALE_SECONDS`, `_GATEWAY_LIVENESS_PREFIX`, `_gateway_liveness_key`, `mark_gateway_connected`, `mark_gateway_disconnected`, `refresh_gateway_liveness`, `get_live_gateway_conn_token`, `is_gateway_connected` (method and module wrapper), and the comment block that explains them (`:40-63`).

`durable_activity.py` (A's final fix round already added the ledger veto next to the pool terms): replace `from core.containers import get_ecs_manager, get_gateway_pool` with `from core.containers import get_ecs_manager`, delete `pool = get_gateway_pool()`, reduce each `if await messaging_repo.has_open_runs(owner_id) or pool.has_active_runs(owner_id) or pool.is_parked(owner_id): continue` to `if await messaging_repo.has_open_runs(owner_id): continue`, delete the `if not await pool.close_idle_user(owner_id): continue` lines and both `get_live_gateway_conn_token` checks with their comments. `tests/unit/services/test_durable_activity.py` already exists (A): drop its `get_gateway_pool` patch and the `get_live_gateway_conn_token` stub; keep both cases.

- [ ] **Step 4: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_durable_activity.py tests/unit/services/test_readiness_state.py tests/unit/services/test_readiness_state_architecture.py tests/unit/routers/test_container_status.py tests/unit/routers/test_container_status_wake_gate.py tests/unit/routers/test_container_status_role_blind_dispatch.py tests/unit/services/test_connection_service*.py tests/unit/test_main_lifespan_shutdown.py -q --no-cov && uv run ruff check core routers`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A apps/backend
git commit -m "feat(backend): readiness probes the container; trial idle reads the ledger; liveness rows and the self-heal dial deleted"
```

---

### Task 9: Backend — delete the pool

**Files:**
- Delete: `apps/backend/core/gateway/connection_pool.py`; tests `tests/unit/gateway/` (whole directory after Task 4's moves), `tests/gateway/`, `tests/unit/core/test_connection_pool_close_drain.py`, `tests/unit/core/test_chat_event_transform.py`, `tests/chaos/fis/_chat_driver.py` (and the chaos test that imports it — `git grep -ln "_chat_driver" tests`)
- Modify: `apps/backend/core/gateway/operator_rpc.py` (delete `gateway_rpc_oneshot` and helpers only it used; keep `perform_signed_connect`), `apps/backend/core/containers/__init__.py`, `apps/backend/main.py:24-27,255-268,395,466,487`, `apps/backend/routers/websocket_chat.py` (if any pool import remains), `apps/backend/core/services/operator_proxy.py` (comments only)
- Test: `apps/backend/tests/unit/test_main_lifespan_shutdown.py`, `tests/unit/observability/test_product_analytics.py` (comment references only)

**Interfaces:**
- Produces: `main._running_count_gauge_loop` on `container_repo`; `core.containers` exports without the pool.

- [ ] **Step 1: Delete and rewrite**

`core/containers/__init__.py`: delete the pool import, `_gateway_pool`, `get_gateway_pool`, `get_gateway_pool_if_started`, `startup_containers`, `shutdown_containers`, and their `__all__` entries. `main.py`: drop the three imports, the `await startup_containers()` line and both `await shutdown_containers()` lines; rewrite the gauge loop:

```python
async def _running_count_gauge_loop():
    """Emit gateway.running.count every 60 s: the P12 backend-liveness canary expects a steady stream."""
    from core.observability.metrics import gauge
    from core.repositories import container_repo

    while True:
        try:
            rows = await container_repo.get_by_status("running")
            gauge("gateway.running.count", len(rows))
        except Exception:
            logger.warning("running_gauges loop iteration failed", exc_info=True)
        await asyncio.sleep(60)
```

Then:

```bash
cd apps/backend && git rm -r core/gateway/connection_pool.py tests/unit/gateway tests/gateway tests/unit/core/test_connection_pool_close_drain.py tests/unit/core/test_chat_event_transform.py tests/chaos/fis/_chat_driver.py
```

(if `tests/unit/gateway` still holds a file whose subject survived, move it first). In `operator_rpc.py` delete `gateway_rpc_oneshot`; keep `perform_signed_connect`, `GATEWAY_PORT` and whatever it uses.

- [ ] **Step 2: Grep gates**

Run:
```bash
cd apps/backend && git grep -n "connection_pool\|get_gateway_pool\|gateway_rpc_oneshot\|GatewayConnectionPool\|send_rpc(" -- core routers main.py scripts tests
```
Expected: no hits except comments that describe history (`product_analytics.py:115`, `run_analytics.py:67`) — rewrite those two comments to not name the pool.

- [ ] **Step 3: Full suite**

Run: `cd apps/backend && uv run python -c "import main" && uv run pytest tests/ -q --no-cov -x --deselect tests/unit/services/test_local_session_relay.py::test_ingestion_is_bounded_ordered_deduplicated_and_emits_only_room_hints`
Expected: PASS (the deselected test is red on `main`; report any other failure with its output — an unrelated one is reported, not fixed).

- [ ] **Step 4: Commit**

```bash
git add -A apps/backend
git commit -m "feat(backend)!: delete the gateway connection pool"
```

---

### Task 10: Clients, infra and docs

**Files:**
- Modify: `packages/shared/src/live/types.ts:147-165,181-187,203-208,236-237,263-268,378-387`, `packages/shared/src/runtime/client.ts:4-6,73-78,99-104`, `packages/shared/src/runtime/types.ts:26-37`, `packages/shared/src/index.ts:61-62,135-136`, `packages/shared/src/runtime/client.test.ts:62-63`
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (W6, W7, W8, W9, W10, W11, W54 alarm blocks; the five dashboard widgets at `:3350-3450`), `apps/infra/test/observability-stack.test.ts:655-700`
- Modify: `openclaw-version.json` (`dev.tag`, `prod.tag`), `.github/workflows/deploy.yml` (regenerated), `CLAUDE.md`

**Interfaces:**
- Produces: the client's `LiveEventType` without the five runtime types; `RuntimeClient` without the two resolve methods.

- [ ] **Step 1: Shared client**

Delete from `live/types.ts`: `QuestionRecord`, `QuestionResolvedEvent`, `RuntimeApprovalResolved`, `RuntimeProgressChanged`, the five union members, the five `LIVE_EVENT_TYPES` entries, the five `LiveEventEnvelope` members and the five `case` arms of the validator. Delete from `runtime/types.ts`: `RuntimeApprovalDecision`, `RuntimeApprovalResolveRequest`, `RuntimeQuestionResolveRequest`. Delete from `runtime/client.ts`: the two interface members, the two implementations, the two type imports. Delete the two `index.ts` export lines each (`:61-62`, `:135-136`). Delete `client.test.ts:62-63` and the assertions that follow them about those two POSTs.

Run: `cd packages/shared && pnpm test && pnpm typecheck && pnpm lint && cd ../../apps/frontend && pnpm exec tsc --noEmit && pnpm lint && cd ../mobile && pnpm typecheck`
Expected: PASS.

- [ ] **Step 2: Observability**

Delete the `this.createAlarm({ id: "W6" … })`, `W7`, `W8`, `W9`, `W10` (metric-math), `W11` and `W54` blocks with their comments; delete the five `GraphWidget`s titled "Gateway connections (open)", "Gateway RPC errors", "Chat messages & errors", "Webchat E2E latency (SLO: p99 < 20s)", "Webchat success rate (SLO: 99.5%)". Leave P12 and every `messaging.*` alarm. In the test file delete the `W2/W8` test's `gateway.rpc.error` entries (keep `container.lifecycle.latency`) and the whole `W9/W10 scope every chat.* leg` test; grep the test file for the other deleted ids and remove those assertions.

Run: `cd apps/infra && pnpm test -- observability && npx cdk synth > /dev/null && git status --short .github/workflows/deploy.yml`
Expected: PASS; commit `deploy.yml` if it changed.

- [ ] **Step 3: Pins**

Set `dev.tag` and `prod.tag` in `openclaw-version.json` to the tag the plugin PR's image build produced (Task 2 ledger entry; verify with `aws ecr describe-images --repository-name lightbulb/openclaw-extended --image-ids imageTag=<tag> --profile isol8-admin --query 'imageDetails[0].imageDigest'`); append one `notes` sentence: "B (2026-09-12): plugin RPC route + agent_end usage hook; backend on container_rpc; the gateway pool is gone. Roll the fleet immediately after each environment's deploy."

- [ ] **Step 4: CLAUDE.md**

Rewrite: line 20 (`…with a persistent WebSocket connection pool on the backend proxying RPC calls…` → `…the backend calls the container's baked lightbulb-channel plugin over HTTP for RPC and readiness, and the container reports run lifecycle and usage back over hook-token routes`); the `main.py` paragraph (`:594`: drop "the running-gauges loop … gateway pool is created lazily on first get_gateway_pool()"; say the gauge loop emits `gateway.running.count` from the containers table and shutdown no longer closes a pool); the Container Orchestration `__init__.py` row (no pool singleton, no startup/shutdown); the Gateway table (`connection_pool.py` row deleted; `operator_rpc.py` row: "`perform_signed_connect` only — the operator-device pairing probe, D's to replace"; add a row for `core/services/container_rpc.py`); the Messaging paragraph A wrote ("it still carries exec approvals, questions and personal-chat usage until sub-project B" → "Control RPCs, readiness (`health`) and abort/steer are HTTP calls into the plugin; non-room usage arrives on `POST /internal/messaging/usage/{ownerId}`"); WebSocket Architecture item 4 ("Backend → container: `core/services/container_rpc.py`, one HTTP request per RPC to the container plugin; the only backend-initiated socket left is the operator-device pairing probe"); Usage Flow step 1 ("A room run's `run.ended` callback, or the plugin's `agent_end` hook for every other run, carries the session's token counts"); the `service-stack.ts` row ("`desiredCount: 1` in both envs until sub-project D moves the machine WebSocket relays off the process; the backend itself holds no per-owner state").

- [ ] **Step 5: Commit**

```bash
git add packages/shared apps/frontend apps/mobile apps/infra openclaw-version.json .github/workflows/deploy.yml CLAUDE.md
git commit -m "chore(stateless-backend): client types, alarms, image pins and docs for the pool deletion"
```

---

### Task 11: Cutover

**Files:** none new. Owner approval is required before each push, merge, deploy or AWS mutation in this task.

- [ ] **Step 1: Branch hygiene and PR**

Rebase the cutover branch onto `main` (which carries the merged plugin PR), run `cd apps/backend && uv run pytest tests/ -q --no-cov` and `cd apps/infra && pnpm test`, push, open the PR with the spec's §3 deletion list in the body, wait for the required check (`claude-review`) and Codex's answer, merge.

- [ ] **Step 2: Dev**

The merge deploys dev. Watch the run (`gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status`). Immediately roll the dev fleet to the pinned image (admin UI or the fleet roll script the A cutover used), redrive `openclaw-work` DLQ if anything dead-lettered during the roll. Then the spec §10 checks on dev: a room turn end to end; a config change from one tab reflected on another within 10 s; a cron run recorded once (`messaging.usage.callback outcome=applied` = 1 for that run, a replay = `replayed`); `/container/status` reports `ready` for a running container and `starting` when its task is stopped by hand; a trial container stops only after its ledger has no open run.

- [ ] **Step 3: Two instances on dev**

```bash
aws ecs update-service --cluster <dev backend cluster> --service <dev backend service> --desired-count 2 --profile isol8-admin
```

Repeat the §10 checks; confirm both tasks served requests (ALB target-group request counts per target); deploy nothing else; set the count back to 1.

- [ ] **Step 4: Prod**

With the owner's explicit approval: `gh workflow run deploy.yml --ref main` after the "0 in-flight runs" precondition, watch it, roll the prod fleet immediately, redrive the DLQ, verify `messaging.rpc outcome=ok` and `gateway.running.count` continue and P12 stays green. `desiredCount` in prod stays 1.

- [ ] **Step 5: Close out**

Delete `docs/superpowers/plans/2026-09-12-stateless-backend.md` and the spec in a follow-up PR (git history keeps them; the PR description is the record), and record the final state in the session memory file.
