# Hermes runtime verification — 10: Authentication & operator/device-trust parity

**Date:** 2026-06-16
**Scope:** How a Hermes container's HTTP API (and WS RPC) authenticates requests, and whether Hermes has any operator/trusted-device equivalent to OpenClaw's.
**Method:** Primary source only, Hermes v0.14.0 at `hermes-agent/` (absolute: `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/hermes-agent`). Every claim cited to file:line read this session. Builds on `docs/audit-2026-05-17-hermes-vs-openclaw.md` (via `git show c9562d39:`); where this reading sharpens or corrects the prior audit it is flagged.

---

## VERDICT (one line)

Hermes auth is **all-or-nothing, single optional shared bearer token, no roles/scopes, no operator role, no device-trust** — and the two remote surfaces (HTTP api_server vs dashboard WS) use **different, unrelated auth mechanisms**. There is **zero** equivalent to OpenClaw's ed25519 operator-device pairing. OpenClaw's desktop pairing + the `operator.admin`-scoped exec-approval trust model **cannot carry over** — it would have to be rebuilt entirely in the Isol8 backend if we want it, or dropped.

---

## 1. How is a request to `/v1/runs` authenticated?

**A single optional global bearer token compared with `hmac.compare_digest`.** Nothing else.

### The validation code (verbatim)

`gateway/platforms/api_server.py:742-762`:

```py
def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
    """
    Validate Bearer token from Authorization header.
    Returns None if auth is OK, or a 401 web.Response on failure.
    If no API key is configured, all requests are allowed (only when API
    server is local).
    """
    if not self._api_key:
        return None  # No key configured — allow all (local-only use)

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if hmac.compare_digest(token, self._api_key):
            return None  # Auth OK

    return web.json_response(
        {"error": {"message": "Invalid API key", "type": "invalid_request_error",
                    "code": "invalid_api_key"}},
        status=401,
    )
```

### Where the key is configured

`gateway/platforms/api_server.py:647`:

```py
self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
```

- **Env var:** `API_SERVER_KEY` (also `API_SERVER_HOST` / `API_SERVER_PORT`, `:642-646`).
- **Config file:** `platforms.api_server.key` — i.e. `config.extra["key"]` (the platform config's `extra` dict; the `:647` getter prefers it over the env var). Default `127.0.0.1:8642` bind (`DEFAULT_HOST`/`DEFAULT_PORT`, referenced at `:642-646`).
- **Default is empty string** → when unset, `_check_auth` returns `None` for every request (`:750-751`): **no auth at all**.

### It is enforced on `/v1/runs` and the SSE endpoint

`_check_auth` is the first statement of every handler. Confirmed call sites:

- `POST /v1/runs` → `_handle_runs`, `api_server.py:2864`
- `GET /v1/runs/{run_id}` → `_handle_get_run`, `:3160`
- `GET /v1/runs/{run_id}/events` (SSE) → `_handle_run_events`, `:3175`
- `POST /v1/runs/{run_id}/approval` → `_handle_run_approval`, `:3225`
- `POST /v1/runs/{run_id}/stop` → `_handle_stop_run`, `:3313`
- also `/v1/chat/completions`, `/v1/responses`, `/v1/models`, `/v1/capabilities`, `/api/jobs/*` (`:945, 971, 1025, 2094, 2378, 2391, 2436, 2451, 2499, 2518, 2551, 2570, 2589, 2608`).

So `/v1/runs` and its SSE stream are authenticated **identically** to every other endpoint: header bearer, or open if no key set.

### SSE token transport — header only, no query-param fallback

`_handle_run_events` (`:3173-3199`) calls `_check_auth(request)` (which reads **only** `request.headers["Authorization"]`) and then opens the `text/event-stream`. There is **no** `?token=` query-param path for SSE.

> **Implication for Isol8 (and CORRECTION vs an OpenClaw assumption):** OpenClaw's Paperclip 401 bug was that the gateway WS only read `?token=` query string while the adapter sent an `Authorization` header (memory `project_paperclip_ws_401_root_cause`). Hermes' HTTP api_server is the **mirror image**: it reads the header and ignores query params. A backend→Hermes client must put the bearer in the `Authorization` header on **every** request, including the long-lived SSE GET. EventSource in a browser cannot set headers, so a browser cannot consume Hermes SSE directly with auth on — only a server-side HTTP client (our backend) can. This is fine for the backend↔container channel but rules out any direct browser→container SSE.

### Hard refusal to bind non-loopback without a key

`gateway/platforms/api_server.py:3433-3451`: if the host `is_network_accessible(...)` (non-loopback) and no key is set, the server **refuses to start** (`:3434-3440`); and if a key is set but too weak (`has_usable_secret(..., min_length=8)` fails) it also refuses (`:3447-3451`). So a network-exposed Hermes api_server is *guaranteed* to have a bearer key — but still only the one global key.

---

## 2. All-or-nothing, or scopes/roles? Any operator-admin / trusted-device?

**All-or-nothing. One key = full access to every endpoint. No scopes, no roles, no operator, no device-trust.**

- The capabilities endpoint self-describes the entire auth model as a single boolean: `gateway/platforms/api_server.py:979-982` → `"auth": {"type": "bearer", "required": bool(self._api_key)}`. There is no scope list, no role enum.
- `/v1/models` `"permission": []` (`:957`) is the cosmetic OpenAI-compat stub, always empty — not an authz mechanism.
- A repo-wide sweep for `operator`, `device_token`/`deviceToken`, `trusted_device`, `ed25519`-device-pairing, per-device scopes found **nothing in the control plane**. The only `ed25519` hits are unrelated: SSH-key denylist paths in `agent/file_safety.py:27` and `agent/context_references.py:26`, and Matrix E2EE Olm/Megolm identity keys in `gateway/platforms/matrix.py:476-540` (a messaging-platform concern, not API auth). The only `nonce` hits are OAuth `state` nonces for provider login (`hermes_cli/auth.py:2513, 5292, 5431`; `hermes_cli/dingtalk_auth.py`) — provider sign-in, not device attestation.
- "role"/"roles" in api_server are LLM **message roles** (user/assistant/system/tool), not authz roles (`:1049-1064, 2696-2706`, etc.). "scope" is **memory/session scoping** (Honcho long-term-memory key, `/new` short-term transcript), not access scope (`:782-794, 871-873`).

So there is no `operator.admin` analogue, no privileged-vs-unprivileged caller distinction, and no notion of a "trusted device" that earns elevated rights. Whoever holds `API_SERVER_KEY` can do everything: start runs, resolve exec approvals, stop runs, CRUD cron jobs.

> **CONTRAST with OpenClaw (confirmed in Isol8 backend this session):** OpenClaw's connect handshake carries `device.{id, publicKey, signature, signedAt, nonce}` for ed25519 device-pairing and returns `auth.{deviceToken, role, scopes, deviceTokens[]}` (audit §1.5, `frames.ts:43-66, 73-125`). Isol8 implements the full client side: `apps/backend/core/crypto/operator_device.py:48` imports ed25519, `:86` requires the `"operator.admin"` scope ("skills.install, agents.create/update/delete, etc."), `:240` signs a v2 connect payload; the `containers` row persists `operator_device_id` + `operator_priv_key_enc` (CLAUDE.md prod-reprovision §). Hermes has **no counterpart to any of this**.

---

## 3. Does the WS channel authenticate differently from the HTTP api_server?

**Yes — completely differently, and they live in different servers.**

There are two distinct WS RPC mount points, neither of which is the api_server:

### `tui_gateway/ws.py` `handle_ws` itself does NO auth

`tui_gateway/ws.py:116-131`: `handle_ws` calls `await ws.accept()` **immediately** (`:118`), emits `gateway.ready` (`:122-131`), then dispatches every inbound JSON-RPC line verbatim through `server.dispatch` (`:163`). There is no token check, no header read, no query param inside `handle_ws`. **All authentication is delegated to the mount point.**

### The dashboard mount gates it with an ephemeral session token + loopback allowlist

`hermes_cli/web_server.py:3384-3401` (`@app.websocket("/api/ws")`):

```py
async def gateway_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403); return
    token = ws.query_params.get("token", "")
    if not hmac.compare_digest(token.encode(), _SESSION_TOKEN.encode()):
        await ws.close(code=4401); return
    if not _ws_client_is_allowed(ws):
        await ws.close(code=4403); return
    from tui_gateway.ws import handle_ws
    await handle_ws(ws)
```

Differences from the api_server bearer:

| | api_server HTTP/SSE | dashboard WS (`/api/ws`) |
|---|---|---|
| Server | `gateway/platforms/api_server.py` (aiohttp), default `127.0.0.1:8642` | `hermes_cli/web_server.py` (FastAPI/uvicorn), default `127.0.0.1:9119` (`:8`) |
| Credential | `API_SERVER_KEY` (env or `platforms.api_server.key`) | `_SESSION_TOKEN` |
| Token nature | **persistent**, operator-configured | **ephemeral random**, `secrets.token_urlsafe(32)` regenerated every process boot (`web_server.py:86`) |
| Transport of token | `Authorization: Bearer` header | `?token=` **query param** (`web_server.py:3390`) |
| Extra gate | non-loopback bind requires a key (`:3434`) | loopback/origin allowlist `_ws_client_is_allowed` (`web_server.py:3163`, `_LOOPBACK_HOSTS` `:3155`) + feature flag `_DASHBOARD_EMBEDDED_CHAT_ENABLED` (`:91`, default `False`) |
| Default exposure | loopback | loopback, dashboard defaults to `host="127.0.0.1"` (`web_server.py:4372`) |

> **CORRECTION / sharpening vs the 2026-05-17 audit:** that audit listed the WS surface's auth as "dashboard session (TBD)" (table in §1.4/§1.5). It is now resolved: the WS is gated by an **ephemeral, per-boot random `_SESSION_TOKEN` passed as a `?token=` query param, plus a loopback/origin allowlist, behind a default-off feature flag** — a *local-dashboard* trust model, not a remote control plane. It is **not** suitable as the backend↔container channel for a SaaS: the token resets every container restart and is never surfaced to the backend, and the whole path is loopback-gated and off by default. The audit's broader point still holds (Hermes *does* have a real WS-RPC surface), but its auth is dashboard-local, not a credential we can provision.

### Bottom line on the channel choice
For a trusted backend→Hermes channel, the **HTTP api_server (`/v1/runs` + SSE) with `API_SERVER_KEY`** is the only viable surface: persistent operator-set credential, designed for "external UIs and orchestrators" (`api_server.py:967-968`), refuses unauthenticated non-loopback binds. The `tui_gateway` WS is a dashboard-local convenience, not a provisionable remote API.

---

## 4. What a trusted backend→Hermes channel looks like, and what CANNOT carry over

### What it looks like
- Container boots Hermes api_server bound on its container IP (non-loopback) with `API_SERVER_KEY` set to a per-container secret (provisioned by the Isol8 backend, e.g. via Secrets Manager / EFS config `platforms.api_server.key`, mirroring how OpenClaw config is written today).
- Backend holds that key, sends `Authorization: Bearer <key>` on **every** request: `POST /v1/runs` to start a turn, `GET /v1/runs/{id}/events` (SSE, header auth) to stream, `POST /v1/runs/{id}/approval` to resolve exec approvals, `POST /v1/runs/{id}/stop` to cancel.
- This **replaces** `connection_pool.py`'s WS-RPC-to-OpenClaw with an HTTP+SSE client. (Architectural detail covered in the runtime/protocol sibling docs; here the point is auth: one bearer, backend-held.)

### What CANNOT carry over (prediction CONFIRMED against source)

1. **Desktop pairing (ed25519 operator-device).** OpenClaw's `device.{publicKey, signature, signedAt, nonce}` handshake and the backend's `operator_device.py` keypair/`operator.admin` scope have **no Hermes endpoint to talk to**. Hermes auth is a flat bearer; there is no connect-time device attestation, no per-device token issuance, no `deviceTokens[]`. The Tauri desktop app's whole pairing flow (`operator_device_id` + `operator_priv_key_enc` on the container row) targets an OpenClaw-only protocol and would be dead against Hermes.

2. **Exec-approval as a *trusted-device* gated action.** Hermes **does** have exec approvals — `POST /v1/runs/{run_id}/approval` with `{choice: once|session|always|deny}`, SSE `approval.request` event (audit §1.3, `api_server.py:3223-3299`). **But** approval resolution is authorized by the **same single bearer** as everything else — there is no notion that "only the trusted operator device may approve." In OpenClaw, exec-approval is bound to the `operator.admin` scope earned via device pairing; in Hermes, anyone with `API_SERVER_KEY` can approve. So the *mechanism* (approve/deny a pending exec) carries over, but the *trust binding* (approval requires a paired, attested operator device) **does not** — it collapses to "the backend holds the key."

3. **Roles/scopes.** Any Isol8 code that reasons about `operator.admin` vs lesser scopes (`operator_device.py:86`) has nothing to map onto. There is exactly one privilege level in Hermes.

### Net
A trusted backend↔Hermes channel is *simpler* than OpenClaw's (one provisioned bearer, header-based, HTTP+SSE) but *flatter*: it loses the device-attested operator model entirely. If Isol8 wants "only this paired desktop may approve dangerous exec," that policy must move **into the Isol8 backend** (e.g. the backend gates which approvals it forwards based on its own operator-device check) — Hermes will not enforce it, because Hermes cannot distinguish callers.

---

## Appendix: files read this session
- `hermes-agent/gateway/platforms/api_server.py` (auth helper `:742-762`; key config `:647`; call sites `:945-3313`; capabilities auth block `:979-982`; non-loopback refusal `:3433-3451`; SSE handler `:3173-3199`; route table `:3419-3423`)
- `hermes-agent/tui_gateway/ws.py` (full — `handle_ws` does no auth, `:116-167`)
- `hermes-agent/hermes_cli/web_server.py` (`/api/ws` mount auth `:3384-3401`; `_SESSION_TOKEN` `:86`; feature flag `:91`; loopback allowlist `:3155-3166`; default host `:4372`)
- `hermes-agent/` repo-wide greps for `ed25519`/`operator`/`device_token`/`nonce`/`scope`/`role`
- `apps/backend/core/crypto/operator_device.py` (`:48, 61, 86, 132-240` — OpenClaw-side operator-device model, for contrast)
