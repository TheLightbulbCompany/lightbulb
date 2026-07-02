# Map 09 — Hermes web server / TUI gateway / dashboard / pairing

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `basis-v0.14.0-spec` (commit 4c46c35e).
All line numbers verified by reading the files in this tree.

## Verdict up front
**None of the web server, tui_gateway WS, or pairing subsystems can substitute
for OpenClaw's control-UI proxy or operator/exec-approval trust model.** The
spec (§"Mechanisms that DON'T carry over", lines 188-193, 410) reaches the same
conclusion and is *accurate* on this surface — it even corrects the upstream
audit. The HTTP `api_server.py` `/v1/*` bearer surface (mapped elsewhere) is the
only provisionable backend↔container channel; everything in *this* map is
loopback-local, default-off, or chat-platform-scoped.

---

## 1. Dashboard web surface + auth (`hermes_cli/web_server.py`)

- **Bind + auth posture.** FastAPI app (`web_server.py:79`). Refuses any non-loopback bind unless `--insecure`/`allow_public` (`start_server`, `:4386-4391`). Auth on `/api/*` is a **single ephemeral `_SESSION_TOKEN = secrets.token_urlsafe(32)` regenerated every process start** (`:86`), injected into the SPA HTML, checked via `hmac.compare_digest` (`_has_valid_session_token`, `:125-142`; `auth_middleware`, `:236-246`). This is a **per-boot, in-process, single-bearer** model — there is no provisioning hook, no scopes, no caller identity. CORS is locked to `localhost`/`127.0.0.1` regex (`:102-107`) and a Host-header allowlist defends DNS-rebind (`:157-233`, GHSA-ppp5-vxwm-4cf7). **Substitutability: zero** — the token dies with the process and is never exposed for an external backend to hold, unlike OpenClaw's provisioned `gateway_token`/`API_SERVER_KEY`.
- **What the REST surface actually does:** config read/write (`/api/config`, `:862-1196`), env-var CRUD + a rate-limited `reveal` of plaintext secrets (`:1242-1269`, 5/30s), model assignment (`/api/model/set`, `:1049`), session list/search over SQLite FTS5 (`:775-836`), and *fire-and-forget action spawns* `gateway restart` / `hermes update` as **detached subprocesses** (`_spawn_hermes_action`, `:666-699`). It is an operator config/observability console for a *local* Hermes install, not a remote control plane.

## 2. The "Chat" tab WS channels are default-OFF and ephemeral-token only

- Four WS endpoints — `/api/pty` (`:3263`), `/api/ws` (JSON-RPC sidecar, `:3384`), `/api/pub` (`:3416`), `/api/events` (`:3445`) — **every one** first checks `if not _DASHBOARD_EMBEDDED_CHAT_ENABLED: ws.close(4403)` (`:3265,3386,3418,3447`). That flag defaults `False` (`:91`) and is only flipped by `hermes dashboard --tui` / `HERMES_DASHBOARD_TUI=1` (`main.py:9660`, arg at `main.py:12314-12318`; `start_server(embedded_chat=…)` `:4377,4382-4383`).
- When enabled, auth is `?token=<_SESSION_TOKEN>` query param + a loopback-client gate `_ws_client_is_allowed` (`:3163-3174`) — browsers can't set `Authorization` on WS upgrade. So even the "real" RPC channel is **the same ephemeral per-boot token, loopback-scoped, behind a default-off flag.** Spec line 191 states this verbatim ("not provisionable") — confirmed.

## 3. tui_gateway WS RPC exposes 68 unauthenticated methods behind the transport gate

- `tui_gateway/ws.py::handle_ws` (`:116-178`) reuses `server.dispatch` **verbatim** (transport.py / ws.py module docstrings), so the WS client gets the *exact* RPC surface Ink-over-stdio uses: 68 `@method`-registered handlers (grep `@method(` = 68) incl. `config.set` (`server.py:3755`), `prompt.submit` (`:3001`), `approval.respond` (`:3730`), `sudo.respond` (`:3720`), `process.stop` (`:4275`), `session.delete` (`:2287`).
- **Critical: `handle_request` does a bare `_methods.get(method)` with ZERO per-method auth, scoping, or caller identity** (`server.py:464-473`). The `@method` registry (`:437-442`) carries no permission metadata. There is no `operator.admin`/`read`/`write` split — OpenClaw's scoped v4 handshake has **no analog**. The only gate is the transport-level `?token=` check upstream in the FastAPI endpoint. A forked, provisionable version would have to invent the entire scoping layer from scratch. This is the structural reason the spec drops device-attested exec-approval for Hermes v1 (§3, lines 189, 395).

## 4. Exec-approval is a side-channel, not a trust binding

- Approval flows through the same dispatch: `approval.request` is **emitted** as an event to the bound transport (`_emit("approval.request", sid, data)`, registered per-session at `server.py:576-578`), and the operator answers via the `approval.respond` RPC (`:3730`) / `sudo.respond` (`:3720`). There is **no notion of "only the paired operator device may approve"** — whoever holds the transport (i.e. cleared the single token) can answer. Spec line 189 ("approval is authorized by the same single bearer … Hermes cannot distinguish callers") is correct. To get OpenClaw-style trusted-device approval, the **Isol8 backend** must gate which approvals it forwards — Hermes provides no primitive.

## 5. DM pairing authorizes *chat users to talk to the bot* — NOT operator devices

- `gateway/pairing.py::PairingStore` (`:76-322`) + `hermes_cli/pairing.py` CLI manage **per-messaging-platform allowlists**: an unknown Telegram/Signal/iMessage user gets a one-time 8-char code (`generate_code`, `:151-192`), the bot owner runs `hermes pairing approve <platform> <code>` (`hermes_cli/pairing.py:64-96`), and the user lands in `{platform}-approved.json`. Security is real but mundane: 1h code TTL, 3 pending max, 10-min rate limit, 5-fail lockout, `chmod 0600` (`:35-45`, `_secure_write` `:50-73`). **`is_approved(platform, user_id)` (`:114-117`) answers "can this chat account message the agent" — it is categorically not device-key attestation** and never touches the backend↔container control hop. Spec line 188's scoping note ("a `pairing` subsystem exists but is a chat-platform DM allowlist, NOT operator-device attestation") is exactly right; do not let a naive `grep pairing` mislead the estimate.

## 6. Net assessment for the spec's scope claims

- The spec's web/tui/pairing conclusions **hold under scrutiny** — this is the one area where verification did NOT reduce confidence in the spec. The load-bearing facts (default-off embedded chat, ephemeral per-boot token, flat 68-method no-scope dispatch, DM-pairing ≠ device trust) are all confirmed at the cited lines.
- **Where the spec may still under-cost (open, not a contradiction):** it shelves `control_ui_proxy` reconstruction as "unquantified" (lines 411, 435). My read reinforces that risk: OpenClaw's control dashboard is an SPA driving an RPC proxy; Hermes' equivalent operator surface is *this* loopback FastAPI dashboard, which is **not designed to be driven by a remote backend at all** (ephemeral token, no provisioning, subprocess-spawning actions). Reconstructing those panels from Hermes `/v1` HTTP endpoints is plausible but the dashboard here gives almost nothing reusable for a multi-tenant remote control plane — so unknown #7 should be treated as real engineering cost, not a paperwork item.
