# 09 — AUTH: the OpenClaw trust/transport-auth chain (verification pass)

**Date:** 2026-06-16
**Scope:** the COMPLETE trust chain for how the Isol8 backend is trusted by, and connects to, a per-user OpenClaw container *today*. Establishes the OpenClaw baseline that a Hermes swap would have to replace. Every claim cites a file:line read in this pass.

> **The single most important thing to internalize:** there are **two physically distinct authenticated hops**, and they use **completely different mechanisms**. Confusing them is the recurring error.
> - **Hop 1 — frontend → API Gateway WebSocket** (the public edge). Auth = Clerk JWT (or Paperclip service-token JWT) in `?token=`, validated by a Lambda authorizer.
> - **Hop 2 — backend → per-user OpenClaw container** (inside the VPC). Auth = a per-container shared secret (`gateway_token`) **plus** an Ed25519 signed-device handshake. The Clerk JWT never travels on this hop.

---

## Topology (explicit)

```
                          HOP 1  (public edge)                 HOP 2  (in-VPC, private)
  ┌──────────┐  wss + ?token=<JWT>   ┌─────────────┐         ┌────────────────────────┐
  │ Browser  │ ───────────────────▶ │ WS API GW    │         │ FastAPI backend (ECS)  │
  │ (Clerk)  │                       │  + Lambda    │         │  GatewayConnectionPool │
  └──────────┘                       │  authorizer  │         └───────────┬────────────┘
                                     │ (Clerk JWT   │                     │ ws://<ip>:18789
  ┌──────────┐  wss + ?token=<svc>   │  OR service- │   $connect/$default │ (token + Ed25519
  │ Paperclip│ ───────────────────▶ │  token JWT)  │ ──VPC Link/NLB/ALB─▶ │  signed-device
  │ adapter  │   &clientId=          └─────────────┘                     ▼  handshake)
  └──────────┘    paperclip-operator                          ┌────────────────────────┐
                                                              │ per-user OpenClaw       │
  ┌──────────┐  wss + ?token=<JWT>                            │ container (gateway      │
  │ Desktop  │   &clientId=node-host  ───────▶ (same edge) ──▶│ :18789, auth.mode=token)│
  │ (Tauri)  │   [PURE RELAY: signs locally]                  └────────────────────────┘
  └──────────┘
```

- **Hop 1** terminates at API Gateway; the authorizer's only job is to decide Allow/Deny and stamp `{userId, orgId, authKind}` into the request context. After that the connection is anonymous-by-edge — the backend trusts the `x-user-id`/`x-org-id`/`x-client-id` headers the `$connect` Lambda injects from authorizer context (`apps/infra/lambda/ws-connect/index.py:16-17,54-59`).
- **Hop 2** is a *separate* outbound WebSocket the backend opens to the container. The browser's Clerk JWT is **not forwarded**; the backend authenticates *itself* to the container with the container's own `gateway_token` + a signed Ed25519 device identity. Per-user identity on this hop is **implicit** — each container is single-tenant, so "who is this" is answered by "which container did we dial" (`apps/backend/core/containers/config.py:739-741`).

---

## (a) Frontend → API Gateway (Hop 1): Clerk-JWT WS authorizer

**Where it sits:** `WebSocketLambdaAuthorizer` attached only to the `$connect` route (`apps/infra/lib/stacks/api-stack.ts:447-457`). Identity source is the query string: `identitySource: ["route.request.querystring.token"]` (`api-stack.ts:448`). So the token rides in the WS URL as `?token=...`, NOT an `Authorization` header (you cannot set headers on a browser `WebSocket` upgrade).

**What the authorizer accepts** (`apps/infra/lambda/websocket-authorizer/index.py:177-276`) — two token kinds, tried in this order:

1. **Paperclip service-token JWT** — HS256, `kind=="paperclip_service"`, verified with a symmetric secret loaded once at cold-start from Secrets Manager (`index.py:88-110,153-174`). Checked FIRST because symmetric verify is far cheaper than a JWKS round-trip (`index.py:206-220`). On success: `principalId=sub`, context `{userId:sub, orgId:"", authKind:"paperclip_service"}`.
2. **Clerk JWT** — RS256, verified against Clerk's JWKS, issuer-checked, `verify_aud:False` (`index.py:222-263`). Org id pulled from the v2 nested `o.id` claim (`index.py:243-245`). On success: context `{userId:sub, orgId:<org or "">, authKind:"clerk_user"}`.

**Output:** an IAM policy (WS APIs require IAM-policy format, not the `isAuthorized` boolean — `index.py:129-150`). Deny → connection rejected at the edge. Allow → the `$connect` integration Lambda reads `requestContext.authorizer.{userId,orgId}` and re-emits them as `x-user-id` / `x-org-id` headers to the FastAPI `/ws/connect` route, plus `x-client-id` copied from `?clientId=` (`apps/infra/lambda/ws-connect/index.py:16-17,27,54-59`).

**Net:** the Clerk JWT's authority ends at the edge. Everything downstream trusts the stamped context. **This hop is 100% Isol8 SaaS-shell and is runtime-agnostic** — it would be byte-for-byte identical whether the container runs OpenClaw or Hermes.

---

## (b) Backend → container (Hop 2): the gateway token + Ed25519 device handshake

This is the OpenClaw-specific hop and the one a Hermes swap must replace.

### The transport token (`gateway_token`)

- **What:** a per-container shared secret, `secrets.token_urlsafe(32)` (`apps/backend/core/containers/ecs_manager.py:1775`; reprovision reuses the existing one or regenerates: `ecs_manager.py:1547-1550`).
- **Minted:** in the backend at provision time (`ecs_manager.py`), NOT in Secrets Manager and NOT a JWT. It is a raw random string.
- **Stored, two places:**
  1. DynamoDB `containers` row, column `gateway_token` (written `ecs_manager.py:579`; read back via `container_repo.get_by_gateway_token` / `get_by_owner_id`). Lookup-by-token GSI exists (`container_repo.py:20-25`).
  2. On EFS inside the container's `openclaw.json` as `gateway.auth = {"mode":"token","token":<gateway_token>}` (`config.py:742-745,756-759`). This is the container side of the shared secret — OpenClaw boots with `auth.mode=token` and accepts that exact string.
- **Rotation:** effectively none today. The field is reused across reprovisions when present (`ecs_manager.py:1550`); `config.py:700-706` notes the `hook_token` indirection was *designed* to leave room for "a future rotate-flow" but no rotation path is wired. A rotate would require rewriting `openclaw.json` on EFS + the DDB row + every paired entry that embeds it (see below) + restarting the container.
- **Why a shared secret and not trusted-proxy mode:** OpenClaw's trusted-proxy mode blocks loopback, which breaks the in-container agent calling its own gateway (`connection_pool.py:391-395`; `config.py:736-741`, cites OpenClaw issue #17761). The transport boundary is the private VPC subnet; the token is the application-layer check.

### Why the token ALONE is insufficient — OpenClaw 4.5 scoped auth

A connect that sends only `auth.token` gets its self-declared scopes **silently cleared** by the server (zero permissions), breaking `sessions.list`, `status`, and most RPCs (`connection_pool.py:493-499`; `operator_device.py:1-13`, citing OpenClaw `message-handler.ts:438-595`). To get real scopes the client must present a **pre-paired Ed25519 device identity** and sign the connect.

### The Ed25519 device identity (the operator device)

- **Keypair:** generated per container at provision (`generate_operator_device`, `operator_device.py:132-135`). The 32-byte seed is **KMS-encrypted** and stored in the DDB `containers` row as `operator_priv_key_enc`, with KMS encryption-context bound to `{owner_id, purpose:"operator-device-seed"}` so a stolen row can't be replayed against another container (`ecs_manager.py:2293,2320-2327`; `operator_device.py:111-122`).
- **Public half pre-registration:** the public key + allowed scopes are written to the container's EFS `devices/paired.json` at provision time (`build_paired_operator_entry`, `operator_device.py:267-308`; written by `ensure_device_identities`, `ecs_manager.py:2329-2372`). A remote operator device that isn't pre-paired is rejected `NOT_PAIRED` (`operator_device.py:166`).
- **The paired entry embeds the gateway_token** inside `tokens.operator.token` (`operator_device.py:300-307`) — so the token and the device public key are co-registered server-side.

### The v4 handshake (what actually validates the token)

Per connection open (`connection_pool.py:490-596`):
1. Container sends `connect.challenge` with a `nonce` (`connection_pool.py:527-534`).
2. Backend KMS-decrypts the operator seed, reconstructs the identity (`connection_pool.py:540-553`).
3. Backend builds the **canonical v2 payload** and signs it (`sign_connect_request`, `operator_device.py:223-264`):
   `v2|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}|{nonce}` (`operator_device.py:187-220`, mirrors OpenClaw `device-auth.ts:buildDeviceAuthPayload`).
   **The `gateway_token` is embedded INSIDE the signed payload** even though it is also sent as `auth.token` — that is the cryptographic binding: stealing token-or-key alone yields no valid signature (`operator_device.py:30-34,515` docstring; `connection_pool.py:512-514`).
4. Backend sends a `connect` req carrying `minProtocol:4,maxProtocol:4`, `role:"operator"`, `scopes:[operator.admin,operator.read,operator.write]`, `auth.token`, the signed `device` dict, and `caps:["tool-events"]` (`connection_pool.py:567-588`).
5. Server rebuilds the v2 string from the request fields, verifies the Ed25519 signature against the pre-paired public key, and the nonce against the challenge → returns `hello-ok` (`connection_pool.py:591-596`; server-side semantics described `operator_device.py:14-34`). The backend then sends a `health` RPC to confirm liveness before starting the reader loop (`connection_pool.py:598-646`).

**Scope note:** the backend claims `operator.admin` (alphabetically first, ordering is load-bearing for signature verify — `operator_device.py:70-89`). Admin short-circuits OpenClaw's authorize fn, unblocking `skills.install`, `agents.create/update/delete`, `secrets.reload`, `cron.*`, `sessions.patch/reset/delete` — the full proxied RPC surface (`operator_device.py:59-89`).

**Protocol/port:** `GATEWAY_PORT = 18789`, `ws://<container_ip>:18789` (`connection_pool.py:25,390`; same in `node_connection.py:42,152`). Container IP discovered via ECS `describe_tasks` (per CLAUDE.md / connection_pool comments), not Cloud Map.

---

## (c) Operator / device trust model — three distinct trusted clients

All three clients connect to the **same** container gateway (`:18789`) and all use the **same v4 challenge/signed-device handshake**, but they differ in *who holds the private key* and *what they gate*:

### 1. Backend operator device (the default path, covered in (b))
- Holds the key (KMS-decrypted server-side), claims `operator.admin`, role `operator`, `clientId="gateway-client"`, `clientMode="backend"` (`operator_device.py:85-97`).
- Gates the entire RPC proxy: chat.send, sessions.*, agents.*, skills.*, etc.

### 2. Paperclip operator-relay (the `?token=` service-token path)
- The Paperclip `openclaw_gateway` adapter connects via the **public WS edge** (Hop 1) with URL `{gateway_url}?token={service_token}&clientId=paperclip-operator` (`apps/backend/core/services/paperclip_adapter_config.py:106`; constant `PAPERCLIP_OPERATOR_CLIENT_ID="paperclip-operator"` at `:42`).
- **The `?token=` here is the Paperclip SERVICE-TOKEN JWT, not the gateway shared secret.** It is minted by `service_token.mint()` — HS256, claims `{sub:user_id, kind:"paperclip_service", exp:+365d, jti}` (`apps/backend/core/services/service_token.py:35-45`), signed with `PAPERCLIP_SERVICE_TOKEN_KEY` (env, sourced from Secrets Manager). The **same** symmetric key the Lambda authorizer loads (`websocket-authorizer/index.py:160-167`; CDK wires both: `api-stack.ts:338-361`). So the service-token authenticates the relay **at Hop 1 / the edge** → authorizer stamps `userId=<owner_id>, orgId="", authKind="paperclip_service"`.
- Inside the backend, `$connect` flags this connection `is_operator_relay` and keeps it OUT of the chat broadcast set (`apps/backend/routers/websocket_chat.py:41,162,176-181`). Its frames flow through a dedicated relay, and the backend itself opens the upstream container connect as `client.id=paperclip-operator, mode=backend` (`websocket_chat.py:300`).
- Each Paperclip agent ALSO gets its own Ed25519 device pre-paired into `paired.json` so its relayed operator connect clears the NOT_PAIRED wall (`ecs_manager.py:2335-2365`; `paperclip_adapter_config.py:34,104` mints `devicePrivateKeyPem`). So on Hop 2 the Paperclip agent presents its own signed device — the service-token is purely the Hop-1 edge credential.

### 3. Desktop / node-host (pure relay — backend holds NO key)
- `clientId ∈ {node-host, openclaw-macos}` (`websocket_chat.py:66`). Connects through the edge (Clerk JWT in `?token=`, `?clientId=node-host`).
- **The backend deliberately does NOT hold the desktop's private key.** Tauri (Rust) + the in-container `openclaw-host` subprocess sign locally with keys in the macOS Keychain (`apps/backend/core/gateway/node_connection.py:1-29`). The backend is a **transparent protocol relay**: on `$connect` it opens the upstream container WS (`role:"node"`), forwards the `connect.challenge` DOWN to the client, the client signs locally and sends the signed `connect` back UP, the backend forwards it verbatim upstream and relays `hello-ok` back (rewriting `res.id` to the client's request id) — `node_connection.py:54-229`. Device id is captured from the *signed* frame, since the client owns the key (`node_connection.py:218-221`).
- **What this gates:** desktop pairing + the **exec-approval** flow (browser-proxy + local command approval). The approval state machine lives backend-side in `connection_pool.py` (`_mark_awaiting_approval`/`_is_true_terminal`, `:326-386`), driven by OpenClaw `agent` events with `stream:"approval"`. The exec-approval *policy* is baked into `openclaw.json` (`config.py:266`, `system.execApprovals.get/set` exposed at `:804-805`).

### Desktop_auth router is NOT the device-trust mechanism
`apps/backend/routers/desktop_auth.py` is a thin wrapper that mints a one-time **Clerk sign-in token** for the desktop WebView (`desktop_auth.py:25-56`). It has **nothing to do** with ed25519 device pairing or exec-approval — those live entirely in `operator_device.py` + `ecs_manager.ensure_device_identities` + `node_connection.py`. (Corrects any assumption that desktop pairing trust is established in `desktop_auth.py`.)

---

## Hermes-swap implications (baseline → target)

- **Hop 1 is untouched** by a runtime swap — it's pure SaaS-shell (Clerk/service-token JWT → authorizer → stamped context). Hermes inherits it for free.
- **Hop 2 is entirely OpenClaw-specific and must be re-implemented for Hermes.** OpenClaw's auth here is: per-container random `gateway_token` + KMS-sealed Ed25519 operator device + `paired.json` + the canonical v2 signed-connect handshake over protocol-v4 `req/res/event` framing. Per the prior audit (§1.2), **Hermes' `api_server.py` uses a single global `API_SERVER_KEY` bearer compared via `hmac.compare_digest` with NO per-tenant scope and NO device/Ed25519 model** (`hermes-agent/gateway/platforms/api_server.py` per audit; not re-read this pass — verify before relying). That means a Hermes swap **deletes the entire operator-device + paired.json + KMS-seed + v2-signed-connect apparatus** and replaces it with a per-container bearer secret. That is *simpler*, but it also removes:
  - the cryptographic token↔key binding (Hermes' bearer is a bare shared secret — single-factor),
  - the scoped-auth model (`operator.admin/read/write`) — Hermes has one bearer = full access,
  - the per-client device identity used by Paperclip-relay + desktop node-host. The desktop "client signs locally, backend never holds the key" relay design (`node_connection.py`) has **no analog** in Hermes' bearer model and would need a fresh design or be dropped.
- **Open question to resolve in the Hermes design:** how do the Paperclip relay and the desktop exec-approval flows authenticate against a Hermes container that only knows a global bearer? Both currently lean on per-client Ed25519 devices that Hermes does not have.
