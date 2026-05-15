# Desktop Node Architecture Migration Design

**Status**: Draft
**Date**: 2026-05-15
**Authors**: prasiddha + Claude

## Summary

Replace the Tauri Rust re-implementation of the openclaw node-host protocol with two cooperating processes per Mac, mirroring upstream openclaw's documented Mac-companion + CLI-node-host split. Tauri keeps macOS-native capabilities (notifications, screen, TCC, optional camera/location). A bundled `openclaw node run` subprocess owns the cross-platform surface (browser, file ops, system.run, exec approvals). Both connect independently to the user's per-container gateway via API Gateway WSS with Clerk JWT auth. Backend pre-pairs both Ed25519 public keys via a new `/api/v1/desktop/pair` endpoint; private keys are generated locally in Tauri and stored in macOS Keychain.

## Problem

The desktop app currently bundles openclaw as a vendored npm package and shells out to a single `openclaw node run` subprocess **only** for `browser.proxy` HTTP forwarding. The remaining 15 node commands (`system.run.*`, `system.execApprovals.*`, `system.notify`, `device.*`, `file.*`, `dir.*`, `screen.snapshot`) are implemented as ~3000 lines of Rust in `apps/desktop/src-tauri/src/` that re-implements the openclaw node-host protocol against our remote AWS gateway.

This re-implementation has been a serial source of upstream-drift bugs:

- v3 → v4 protocol bump (PR #660, #662): our Rust client hardcoded protocol v3; openclaw 2026.5.12 raised the floor to v4; chat broke for everyone until we discovered + fixed.
- openclaw 2026.4.5 → 2026.5.12 split Discord into a separate workspace package; our launcher kept failing to load `@buape/carbon` until we bumped the vendor (PR #664).
- 2026.5.x changed `openclaw node run` semantics from "binds local gateway + browser HTTP" to "connects to remote gateway as a node-host client"; our launcher invokes the same command but with the old expectations. **This is the currently-broken state**: `browser.proxy` fails with `ECONNREFUSED 127.0.0.1:18789` because the subprocess tries to connect to a local gateway that doesn't exist.

The root issue is that we ported a copy of upstream's macOS Swift app pattern into Rust but didn't faithfully follow upstream's CLI-node-host model alongside it. Upstream's documented architecture for "remote gateway mode" (our use case) is **two cooperating processes** — a native Mac companion AND a CLI node-host — communicating via the gateway hub. We collapsed them into one Rust impl and re-invented half of what the CLI already does.

## Goals

1. Eliminate desktop re-implementation of openclaw node-host protocol. The bundled subprocess **is** upstream openclaw; protocol/CLI changes inherit automatically.
2. Preserve current user-visible UX: Isol8 branding, in-app chat webview, Clerk auth, auto-update via existing pipeline, macOS-native notification UX, TCC permission introspection.
3. Fix the currently-broken `browser.proxy` flow as a natural consequence of the migration (no separate band-aid PR).
4. Improve security posture beyond upstream's local-trusted-operator threat model where SaaS context warrants it: client-generated keys (not backend-minted), macOS Keychain at-rest storage, live-session revocation, log redaction of JWT in query strings.
5. Gain capabilities that upstream's Mac companion exposes (camera, screen.record, location, canvas) — opt-in, not forced.

## Non-goals

- Bigger architectural changes (e.g., dropping Tauri for Electron, dropping the in-app webview for browser-only chat). User explicitly retained Isol8 branding + in-app chat as constraints.
- Forking upstream's macOS Swift app. We mirror its **pattern**, not its code.
- Cross-platform desktop (Windows/Linux) support in this migration. Mac-only.
- Multi-org-per-user support. Memory `project_single_org_per_user` constrains us to one org per user; context switching between personal/single-org is supported, simultaneous multi-context is not.

## Constraints

- Keep Isol8 brand: app icon, bundle id `co.isol8.desktop`, signed certs, auto-update channel.
- Keep chat in-app webview pointing at `dev.isol8.co/chat`.
- Keep Clerk auth (no openclaw QR-pairing flow for end users).
- Stay on Tauri (no Electron migration).

## Architecture

### High-level

Two processes run on each Mac inside the Tauri `.app` bundle:

**Tauri Rust process** (the Mac companion node)
- Hosts the chat webview (unchanged)
- Owns macOS-native capabilities: `system.notify`, `screen.snapshot`, `device.info`, `device.status`, `device.health`, `device.permissions`, optional `canvas.*`, `camera.*`, `screen.record`, `location.get` if enabled
- Generates and persists Ed25519 keypairs in macOS Keychain (one for itself, one for the CLI subprocess to use)
- Bootstraps + supervises the CLI subprocess
- Runs a tiny local WSS proxy (loopback) so the subprocess can connect to the gateway via Tauri-managed JWT injection — see "JWT injection" below
- Speaks openclaw protocol v4 directly over WSS to API Gateway with Clerk JWT in URL query (`?token=<jwt>`)
- Advertises `clientId: "openclaw-macos"` (upstream-allowlisted — see `src/gateway/protocol/client-info.ts:3-17`), `clientMode: "node"`, `role: "node"`, `displayName: "<User> | Isol8 Mac"`

**openclaw CLI subprocess** (the node-host)
- Runs `openclaw node run` from the bundled npm package, **unmodified**
- Owns cross-platform capabilities: `system.run.*`, `browser.proxy`, `file.fetch`, `file.write`, `dir.list`, `dir.fetch`, plus upstream plugin commands inherited from `browser` and `file-transfer` extensions. (`system.execApprovals.*` are NOT node-invoke commands — they go through a separate `exec.approvals.node.*` gateway method.)
- Loads its Ed25519 identity from `OPENCLAW_HOME/identity/device.json` (upstream-canonical path — see `src/infra/device-identity.ts` + `src/config/paths.ts`), written by Tauri at spawn time, source-of-truth in Keychain
- Connects to **Tauri's local WSS proxy** (`ws://127.0.0.1:<random_port>/`) — Tauri proxies frames to/from `wss://ws-dev.isol8.co/?token=<jwt>` with JWT injection
- Advertises `clientId: "node-host"` (upstream-allowlisted), `clientMode: "node"`, `role: "node"`, `displayName: "<User> | Isol8 Desktop (CLI)"`

Both processes appear in `node.list` as distinct entries with distinct nodeIds. `clientId` is exposed in the schema (`buildEffectiveKnownNode` in `node-catalog.ts:136-169`), so the agent can distinguish "Mac companion" from "CLI node-host" without fuzzy-matching displayNames.

### JWT injection (the Tauri local WSS proxy)

The unmodified `openclaw node run` CLI constructs its WSS URL as `${scheme}://${host}:${port}` with no support for query string params (`src/node-host/runner.ts:216-219`). But our `apps/infra/lambda/websocket-authorizer/index.py` reads Clerk JWT from `queryStringParameters["token"]` at `$connect`. Subprocess cannot put `?token=<jwt>` in its outbound URL, so it would get rejected at $connect before any frames are exchanged.

Resolution: Tauri runs a small loopback WSS proxy. Subprocess connects to it (no auth needed locally); Tauri opens the real upstream connection with JWT in URL and forwards frames in both directions.

```rust
// apps/desktop/src-tauri/src/proxy.rs — new file, ~80 lines
// 1. Bind ws://127.0.0.1:0 on app launch (random port)
// 2. On accept: Tauri opens wss://ws-dev.isol8.co/?token=<current_jwt>
// 3. Bidirectional frame forwarding (subprocess ↔ proxy ↔ API Gateway)
// 4. On JWT rotation: close upstream half, reopen with new token; subprocess
//    sees a transient disconnect + reconnect, no restart needed
// 5. On proxy shutdown: close subprocess connection cleanly
```

Why this works:
- Subprocess sees a normal WS server; speaks openclaw protocol unmodified.
- API Gateway sees the normal Clerk JWT in URL query at $connect; Lambda authorizer validates as today.
- Tauri owns JWT lifecycle (rotation is transparent to subprocess).
- Single API Gateway ingress — no new infra.
- The proxy is pure transport-layer plumbing (~80 lines), not a re-implementation of the openclaw protocol. The subprocess is genuinely unmodified upstream code.

### Session binding (two-node-aware)

The existing backend pins each chat session to a specific exec target via `sessions.patch { execNode: ... }` in `apps/backend/routers/websocket_chat.py`. Today's logic looks up `_user_nodes[user_id]` which assumes one node per user — with two nodes (Tauri-mac + CLI subprocess), whichever connects last wins, so `host=node` exec could land on the Tauri-mac node that doesn't advertise `system.run`.

`tools.exec.host` stays at upstream's default `"auto"` (which resolves to `gateway` when no sandbox is active). Agent explicitly opts into `host=node` per-call when it needs the user's Mac. We do **not** globally pin `tools.exec.node` in the container's `openclaw.json`, because in an org context the container is shared by multiple members — pinning to one member's deviceId would force everyone's exec to that one Mac.

Instead, we reuse upstream's per-session `execNode` mechanism (the existing `sessions.patch { execNode: <deviceId> }` call), extending our backend's user→device mapping to know about both kinds. The `clientId` field on each `PairedDevice` (set by each process during its connect handshake) is how we slot a paired device into the right bucket:

```python
# core/services/node_proxy.py
# Before: _user_nodes[user_id] = { nodeId, connection_id, owner_id }
# After:  _user_nodes[user_id] = {
#           "cli": { nodeId, connection_id, owner_id, clientId: "node-host" },
#           "mac": { nodeId, connection_id, owner_id, clientId: "openclaw-macos" },
#         }

def get_user_cli_node(user_id: str) -> dict | None: ...
def get_user_mac_node(user_id: str) -> dict | None: ...

# core/routers/websocket_chat.py session binding:
# When user X opens a chat session, call openclaw's native:
#   sessions.patch { sessionKey: ..., execNode: _user_nodes[X]["cli"].deviceId }
# Always pins to the CLI device, never overwritten by the Tauri-mac connection.
```

Division of labor between upstream and Isol8 layers:

| Concern                                | Owner            | How                                                                 |
|----------------------------------------|------------------|---------------------------------------------------------------------|
| Per-session exec pinning               | upstream-native  | `sessions.patch { execNode: <deviceId> }` — `params.boundNode` flows through `resolveNodeExecutionTarget` (`bash-tools.exec-host-node-phases.ts:96-127`) |
| Distinguishing the two devices on one Mac | upstream-native  | `clientId` field on `PairedDevice` and in `node.list` output (`"openclaw-macos"` vs `"node-host"`) |
| User → deviceId lookup (multi-tenant) | Isol8 backend    | `_user_nodes[user_id][kind]` in-memory map, populated from each process's connect handshake |

Openclaw itself is single-user — there's no native concept of "which device is whose." That's the part our backend layers on top, because the container is shared across an org's members.

For non-exec plugin commands (browser, file, screen), routing model:

- **Per-call selection by the agent**, not auto-routing. `node.invoke` (`src/gateway/server-methods/nodes.ts:1048-1101`) requires an explicit `nodeId` from the caller; the gateway only validates that the chosen node advertises the command. There is no gateway-side "route by command name" mechanism.
- **Personal owner**: agent calls `nodes.list`, sees two entries, picks the one whose `clientId` matches the command class (CLI for browser/file/screen, Mac for notify/device/screen.snapshot). Simple.
- **Org owner**: multiple members each have a CLI node advertising browser/file/screen. Agent must pick the CLI deviceId of the *requesting* user. Existing pattern: container config sets `nodes.browser.mode = "manual"` for orgs (`apps/backend/core/containers/config.py:660-668`); the migration extends this to fileTransfer + screen (see "Backend changes" below). Agent gets the requesting user's CLI deviceId from session metadata (Clerk identity → backend-injected `requesterCliNodeId` field — added to the session-binding step alongside `execNode`).

Result for routing:
- Personal owner: their session pins to their CLI node; exec on node = their Mac. Agent picks Mac vs CLI for non-exec via clientId on `nodes.list` entries.
- Org member Alice: her session pins to her CLI node; her exec = her Mac. Bob's session pins to his.
- Channel-triggered run (no member): execNode unset; agent must specify `node=<id>` per-call or stay on gateway.

### Connection path / handshake bridge

Native `openclaw node run` is server-first: the client opens WSS, waits for `connect.challenge` from the server, signs the nonce, then sends `connect`. Our existing API Gateway WSS path is client-first because of API Gateway's Management-API GoneException race (~37ms post-`$connect` window where pushes silently fail) — Tauri Rust sends `connect` first, backend opens upstream to container, signs the container's challenge using the per-member EFS-stored device key, returns `hello-ok`.

If we removed the backend bridge entirely, the subprocess would connect, wait for a challenge that API Gateway can't reliably push during the GoneException window, and time out.

**The bridge stays — but its role changes.** Backend no longer signs upstream challenges itself (it doesn't have the subprocess's private key). Instead:

1. Subprocess connects to Tauri's loopback WSS proxy (`ws://127.0.0.1:<random_port>/`). Tauri's proxy opens upstream to `wss://ws-dev.isol8.co/?token=<jwt>` (API Gateway, Lambda authorizer validates). Tauri-mac process connects directly to the same upstream URL with the same JWT.
2. Backend's `handle_node_connect` opens upstream WS to the user's container with `role: "node"`.
3. Container sends `connect.challenge` upstream with a fresh nonce.
4. Backend forwards the challenge to the subprocess via Management API push, with a ~150ms post-`$connect` delay to avoid the GoneException window.
5. Subprocess signs the nonce with its locally-held private key. The signature payload **includes the container's `gateway.auth.token`** in `connect.params.auth.token` (openclaw's v2 signature payload binds the token, so it has to be present at signing time — see `core/gateway/node_connection.py:255-263`).
6. Subprocess sends signed `connect` back via `$default` route.
7. Backend forwards the signed `connect` upstream to the container.
8. Container validates: outer `auth.token` matches its `gateway.auth.token`, AND signature against pre-paired pubkey covers nonce + token + identity claims. Returns `hello-ok` upstream.
9. Backend proxies `hello-ok` to the subprocess.

**Container `gateway_token` ships to the desktop at pair time.** Today the backend holds the container's `gateway_token` privately and mutates `connect_params["auth"]` to inject it before signing — that no longer works once the CLI signs locally. Fix: `/api/v1/desktop/pair` response includes `gatewayToken` alongside `gatewayUrl` and `deviceIds`. Tauri persists it in Keychain (entry `co.isol8.desktop.container.gateway_token`) and passes it to the subprocess via the upstream-supported `OPENCLAW_GATEWAY_TOKEN` env var (per `docs/nodes/index.md`: "Env vars are preferred: `OPENCLAW_GATEWAY_TOKEN` / `OPENCLAW_GATEWAY_PASSWORD`"). Subprocess's openclaw config picks it up, includes it in `auth.token` before signing.

Security note on `gateway_token` distribution:
- The token is per-container (per-owner), not per-user. Members of an org already have lawful access to that container; receiving its `gateway_token` doesn't expand their existing access.
- Token alone is insufficient to act as any specific member — the device-signature layer still requires the right private key.
- Token rotates when the container is reprovisioned; backend would re-ship via `/api/v1/desktop/pair` (idempotent endpoint, called on every JWT refresh anyway).

The bridge becomes a pure protocol relay — no key material on backend, just frame-forwarding with a small delay to defeat the API Gateway timing race. If the 150ms delay turns out fragile in practice, fallback options:

- Subprocess wrapper sends an empty `ping` frame first, triggering backend's relay path before challenge arrives. (Requires modifying spawn behavior.)
- Move subprocess to a dedicated WSS endpoint that bypasses API Gateway Management API entirely (ALB host-routed to user's container). (Larger infra change.)

Both deferred to implementation if needed; v1 takes the simple delay approach.

### Process supervision

- Tauri starts the loopback WSS proxy at app launch (binds `ws://127.0.0.1:0`, captures the assigned port).
- Tauri spawns subprocess on first Clerk JWT receipt (after webview sign-in), passing `--host 127.0.0.1 --port <proxy-port>` so the subprocess targets the local proxy. Env: `OPENCLAW_HOME=<app-support>/co.isol8.desktop/openclaw`, `OPENCLAW_GATEWAY_TOKEN=<container-gateway-token>`.
- Subprocess crashes → Tauri respawns with same identity, exponential backoff.
- Tauri exit → subprocess killed via `kill_on_drop`; proxy shuts down on Tauri Drop.
- JWT rotation: Tauri's proxy reopens its upstream connection with the fresh JWT; subprocess sees a transient transport blip (handled by openclaw's native reconnect/backoff). No subprocess restart.

### Identity & pairing

Per-Mac identity:
- Tauri-mac private key: stored in macOS Keychain entry `co.isol8.desktop.device.mac`
- CLI subprocess private key: stored in macOS Keychain entry `co.isol8.desktop.device.cli`
- Public keys + deviceIds = `SHA-256(pubKey)` derived at use time

Pair flow:
1. User signs into Tauri webview via Clerk.
2. Webview emits Clerk JWT to Tauri Rust (existing `send_auth_token` flow).
3. Tauri checks Keychain. Missing → generate both Ed25519 keypairs locally.
4. Tauri POSTs `/api/v1/desktop/pair` with `{tauriMacPubKey, cliPubKey, commandsMac[], commandsCli[]}` and Clerk JWT bearer.
5. Backend:
   - Validates Clerk identity, resolves `(user_id, owner_id)` from JWT context
   - Writes both public keys to `<owner_container>/paired.json` with `role: "node"` and respective command sets
   - Asserts the two command sets are disjoint before writing
   - Returns `{gatewayUrl, deviceIdMac, deviceIdCli}`
   - Idempotent: if entries already exist with matching pubkeys, no-op
6. Tauri spawns subprocess. Subprocess loads identity, connects, gateway accepts pre-paired key.
7. Tauri-mac process connects in parallel (existing WSS path, just simplified protocol — no more bridging custom Rust handlers).

Context switching (personal ↔ org):
- Clerk webview emits new JWT with new org context
- Tauri detects org_id claim change → triggers re-pair against new context
- Backend idempotently writes user's existing pubkeys to new owner's paired.json
- Tauri kills + respawns subprocess with new JWT
- Both processes reconnect to new container

### Revocation

`DELETE /api/v1/desktop/devices/{device_id}` (Clerk-authed):
- Removes the pubkey entry from the owner's container `paired.json`
- **Force-closes** the desktop's active API Gateway WSS connection for that deviceId (so any in-flight session terminates immediately, not just future reconnects)
- Returns 204

The force-close requires a deviceId ↔ API Gateway connectionId mapping that doesn't exist today. To add: `node_proxy.py` tracks `(deviceId → ws_connectionId)` when a paired device connects; revocation looks up by deviceId and calls API Gateway's `@connections` DELETE.

Trigger paths:
- User explicitly revokes from settings UI (future feature)
- Webhook on Clerk org-membership-removed → backend iterates user's devices in that org's container, calls revoke
- Future security event response

This is the HIGH-severity revocation fix from the security review: today, removing a paired entry only blocks future reconnects; live sessions keep going until they drop.

### Capability assignment (disjoint by design)

| Command                          | Owner             | Implementation                                           |
|----------------------------------|-------------------|----------------------------------------------------------|
| `system.run`, `system.run.prepare`, `system.which` | CLI subprocess | Upstream `NODE_SYSTEM_RUN_COMMANDS`           |
| `browser.proxy`                  | CLI subprocess    | Upstream `extensions/browser/`. Note: in remote-gateway mode the Mac companion does NOT advertise `browser` (`MacNodeModeCoordinator.swift:143-145` gates it on `connectionMode == .local`), so the CLI is the only path — matches upstream's intended split. |
| `file.fetch`, `file.write`       | CLI subprocess    | Upstream `extensions/file-transfer/`                      |
| `dir.list`, `dir.fetch`          | CLI subprocess    | Upstream `extensions/file-transfer/`                      |
| `system.notify`                  | Tauri-mac         | UNUserNotificationCenter binding (existing Rust)         |
| `screen.snapshot`                | Tauri-mac         | ScreenCaptureKit (existing Rust)                          |
| `device.info`                    | Tauri-mac         | Sysctl + IORegistry (existing Rust)                       |
| `device.status`                  | Tauri-mac         | Uptime + battery (existing Rust)                          |
| `device.health`                  | Tauri-mac         | Thermal + power (existing Rust)                           |
| `device.permissions`             | Tauri-mac         | TCC permission introspection (existing Rust)              |

**`system.execApprovals.get/set` are NOT in this table.** Although built into the CLI node-host as `NODE_EXEC_APPROVALS_COMMANDS`, they're not routable via `node.invoke` — `server-methods/nodes.ts:1074-1085` rejects them with `INVALID_REQUEST`. The agent's exec-approval interactions go through a separate gateway method (`exec.approvals.node.*`), which is wholly upstream-internal. Our backend doesn't proxy or advertise these commands; openclaw handles them end-to-end inside the subprocess.

Future opt-in (Tauri-mac, upstream-shaped):
- `camera.list`, `camera.snap`, `camera.clip` — AVCaptureSession
- `screen.record` — ScreenCaptureKit recording
- `location.get` — CLLocationManager
- `canvas.*` — WKWebView panel

These are NOT in v1 scope. Listed for forward-compatibility.

### Backend changes

New endpoints:
- `POST /api/v1/desktop/pair` (Clerk-authed): idempotent pair flow described above. Request: `{tauriMacPubKey, cliPubKey, commandsMac[], commandsCli[]}`. Response: `{gatewayUrl, deviceIdMac, deviceIdCli, gatewayToken}` — the `gatewayToken` is the container's pre-shared `gateway.auth.token` needed by the subprocess to sign its connect.
- `DELETE /api/v1/desktop/devices/{device_id}` (Clerk-authed): revoke + live-kick

Modified:
- `core/services/node_proxy.py`:
  - Stop generating device keys server-side; clients own keys now.
  - `_user_nodes[user_id]` becomes `{cli, mac}` keyed by `clientId`; `get_user_cli_node()` / `get_user_mac_node()` accessors. Connect handler reads the connecting process's `clientId` from the handshake to slot the deviceId into the right bucket.
  - Keep WSS relay to container.
  - Add `force_close_device(device_id)` for revocation kick.
- `core/gateway/node_connection.py`: simplify the v2 signature path — backend no longer constructs `_build_device_identity` (subprocess and Tauri Rust both sign locally). The bridge becomes a pure protocol relay: it receives `connect.challenge` from upstream, pushes to client via Management API with a ~150ms post-`$connect` delay, forwards the client's signed reply upstream. Most code deletes, but the relay scaffolding stays.
- `core/containers/config.py`:
  - `ensure_node_paired_entry`: accept public key directly instead of deriving from server-stored private key.
  - `write_openclaw_config`: when `owner_type == "org"`, extend the existing `nodes.browser.mode = "manual"` pattern to `nodes.fileTransfer.mode` and `nodes.screen.mode`. Personal owners keep `auto`.
- `routers/websocket_chat.py`: session-binding logic updates `_user_nodes[user_id]["cli"].deviceId` (not the unscoped `nodeId`) when calling `sessions.patch { execNode: ... }`. Stops being overwritten by the Tauri-mac connection.

Deleted:
- `_generate_member_device_key` and EFS device-key persistence under `<efs>/<owner_id>/devices/<user_id>/.node-device-key.pem` — Keychain on user's Mac is the source of truth now.
- Per-member device-key reading logic across `node_connection.py`.

CI contract test:
- New: backend boots a real upstream openclaw gateway (Node subprocess), writes a synthesized `paired.json`, attempts node connect with a known Ed25519 keypair. Asserts the gateway accepts the pre-paired device. Runs on every openclaw upstream bump. Memory `feedback_audit_openclaw_protocol_version_on_upgrade` codified.

### Desktop changes

Deleted (~2000 LOC):
- `apps/desktop/src-tauri/src/node_client.rs` (478 lines) — subprocess does the WS now
- Most of `apps/desktop/src-tauri/src/node_invoke.rs` (~1000 of 1134 lines) — keep only Mac-native command handlers
- `apps/desktop/src-tauri/src/file_transfer.rs` (600 lines) — file-transfer plugin in subprocess
- `apps/desktop/src-tauri/src/exec_approvals.rs` (591 lines) — framework-native in subprocess
- `apps/desktop/src-tauri/src/browser_sidecar.rs` (150 lines) — subprocess directly handles browser

Kept + modified:
- `apps/desktop/src-tauri/src/main.rs`: spawns subprocess after Clerk auth, supervises lifecycle, kills on JWT-context change
- `apps/desktop/src-tauri/src/lib.rs`: adds Keychain integration, subprocess supervisor, pair-endpoint client
- `apps/desktop/src-tauri/src/node_invoke.rs`: trimmed to Mac-native commands only (`system.notify`, `device.*`, `screen.snapshot`)
- New: `apps/desktop/src-tauri/src/keychain.rs` — Ed25519 keypair gen + Keychain wrap (~150 LOC)
- New: `apps/desktop/src-tauri/src/subprocess.rs` — supervisor with JWT rotation, crash respawn, identity bootstrap (~200 LOC)

Vendor:
- `apps/desktop/src-tauri/scripts/vendor-sidecars.sh` already at openclaw 2026.5.12 + Node 22.16.0 (PR #664). No further changes needed beyond removing now-unused launcher shim path; subprocess invocation becomes `openclaw node run --host ws-dev.isol8.co --port 443 --tls`.

Launcher shim simplification:
- Current shim is a bash script that spawns `node openclaw.mjs node run --host 127.0.0.1 --port 18789`. The hardcoded host/port were the bug we hit; replace with reading env vars Tauri provides (`ISOL8_GATEWAY_HOST`, `ISOL8_GATEWAY_PORT`).

### Bundle size

Approximate before/after for the Apple Silicon DMG:

- Today (v0.2.19): ~750 MB (Tauri + WebView + bundled Node + openclaw npm + 3000 LOC Rust)
- After: ~400 MB (Tauri + WebView + bundled Node + openclaw npm + ~600 LOC Rust)

The savings come from compiled Rust shrinking; Node + openclaw npm dominates the bundle.

## Security posture

Findings from the auth review (sub-agent verification) are baked into the design:

| Finding                                     | Severity | Mitigation                                                                 |
|---------------------------------------------|----------|----------------------------------------------------------------------------|
| Backend mints private keys                  | HIGH     | Clients generate keypairs locally; backend only sees public keys           |
| Revocation doesn't kick live sessions       | HIGH     | `force_close_device` called on revoke; ws connections terminated           |
| Flat-file private key storage               | MEDIUM   | macOS Keychain replaces flat JSON for both keypairs                        |
| Clerk JWT in WSS URL query                  | MEDIUM   | Audit + redact in API Gateway access logs + Lambda authorizer CloudWatch    |
| Cross-process key pivot                     | MEDIUM   | Backend asserts disjoint command sets when writing paired.json; runtime test |
| Upstream protocol drift                     | MEDIUM   | CI contract test boots real upstream gateway on every openclaw bump        |
| Nonce replay protection (handshake)         | LOW      | Already sound in upstream openclaw — no action needed                       |
| DeviceId collision (SHA-256 of pubkey)      | LOW      | Cryptographically negligible; no action needed                              |

## Data flow

### Normal browser.proxy invocation (after migration)

```
1. Agent in container: "browse to resy.com" tool call
2. Container's openclaw gateway emits node.invoke.request{ command: "browser.proxy", ... }
3. Gateway looks up: "browser.proxy" is owned by deviceId<cli>
4. Gateway forwards over the active node WS to "isol8-node" subprocess (which is at: container → AWS WS uplink → API Gateway → backend relay → Tauri-side subprocess connection)
5. Subprocess's browser plugin (upstream extensions/browser) handles the request: Chromium nav + DOM extraction
6. Subprocess sends node.invoke.result back over the same WS path
7. Agent receives result
```

No local HTTP 18791 server. No bash launcher trying to parse stdout. No Tauri Rust HTTP client timing out. Everything goes through the gateway.

### Normal system.notify invocation (after migration)

```
1. Agent in container: "tell user X" notification
2. Gateway forwards node.invoke.request{ command: "system.notify", ... } to deviceId<mac>
3. Tauri Rust's invoke handler receives via its WS → calls UNUserNotificationCenter
4. macOS shows native notification
5. Tauri Rust responds via WS
```

Same paths Tauri Rust uses today. No change in UX.

## Risks

**R1: Upstream openclaw breaks the `node run` CLI shape again.**
- Likelihood: medium (it changed substantially from 2026.4.5 to 2026.5.x).
- Impact: subprocess fails to connect or fails on commands.
- Mitigation: CI contract test on upstream bump; we control the pin in `openclaw-version.json` and `vendor-sidecars.sh`; v4 protocol enforcement on connection negotiates failure clearly.

**R2: Keychain integration in Tauri has cross-version compat issues.**
- Likelihood: low (mature crate `keyring` or `security-framework` exists in Rust).
- Impact: keys lost on macOS upgrade or after Tauri reinstall.
- Mitigation: standard Keychain access groups; survives app updates; for catastrophic loss, user re-pairs (idempotent endpoint handles it).

**R3: JWT rotation while subprocess is active drops connection.**
- Likelihood: high (Clerk rotates every ~50min).
- Impact: brief disconnect during rotation.
- Mitigation: upstream openclaw's `GatewayClient` supports in-flight token refresh via config update. Tauri sends new token over subprocess stdin; subprocess updates its config and reconnects with fresh creds. ~2 sec gap per rotation, well within user tolerance for chat.

**R4: Two-node node.list UX confusion.**
- Likelihood: medium (users might wonder why their Mac shows twice).
- Impact: minor confusion in admin/dev panels.
- Mitigation: distinct displayNames ("Isol8 Mac" vs "Isol8 Desktop (CLI)") make ownership obvious; backend node.list endpoint could group them by parent device in future UI work.

**R5: Org context-switching corner case during in-flight invoke.**
- Likelihood: low (users don't switch orgs often, and a single invoke completes in seconds).
- Impact: in-flight invoke may fail when subprocess is killed mid-call during context switch.
- Mitigation: subprocess shutdown is graceful with a 1-second drain; ongoing invokes complete or return a clean error. Agent retries acceptable.

**R6: Operator-noticed gotchas from upstream (subagent verification).**
- Default `displayName` is the machine name on both processes; we MUST set explicit distinct names (`<User> | Isol8 Mac`, `<User> | Isol8 Desktop (CLI)`) or they're indistinguishable in `nodes.list`.
- Two pairing prompts at first run for upstream operators — we sidestep via backend pre-pairing, but two paired entries exist per Mac.
- Separate `exec-approvals.json` stores (CLI's `~/.openclaw/exec-approvals.json` and Mac companion's in-app store). For our split where browser/exec only flow through the CLI side, the Mac side's approvals store is unused. **Leave `OPENCLAW_NODE_EXEC_HOST` UNSET**; setting it to `"app"` would re-couple CLI exec to the Tauri-mac process (CLI's `system.run` would fail if Tauri-mac is down).
- `isLocalMacNode` heuristic in upstream `nodes-utils.ts:74-80` matches `nodeId.startsWith("mac-")` but real deviceIds are SHA-256 hex — so `pickDefaultNode`'s "prefer local Mac" tiebreaker never fires. Doesn't affect us because we use per-session `execNode` binding, not auto-pick, but worth noting if we ever rely on auto-pick.

**R7: macOS App-Translocation breaks subprocess paths on first download.**
- Likelihood: medium (Apple's translocation can put .app at `/private/var/folders/.../`).
- Impact: subprocess fails to find bundled node/openclaw assets.
- Mitigation: existing launcher already uses `$(dirname "${BASH_SOURCE[0]}")` for path discovery, which works under translocation; verify in integration test.

## Migration plan summary

**Order of work** (each item is one PR; total estimate 1-2 weeks):

1. Backend: new `/api/v1/desktop/pair` endpoint + DDB plumbing
2. Backend: `force_close_device` for revocation kick + `/api/v1/desktop/devices/{id}` DELETE
3. Backend: CI contract test against real upstream openclaw gateway
4. Backend: simplify `core/gateway/node_connection.py` once desktop migrates (later)
5. Desktop: Keychain integration (`keychain.rs`), keypair generation
6. Desktop: subprocess supervisor (`subprocess.rs`), JWT relay
7. Desktop: trim `node_invoke.rs` to Mac-native commands only
8. Desktop: delete `node_client.rs`, `file_transfer.rs`, `exec_approvals.rs`, `browser_sidecar.rs`
9. Desktop: pair-flow integration with backend
10. End-to-end: integration test against dev environment
11. Ship v0.3.0 (major version bump signals new architecture)

Each PR is independently revertable. After step 8, the desktop binary is fundamentally different — that's a one-way door; revert means going back to v0.2.x.

## Open questions / deferred decisions

- **D1**: Should we use `keyring` (cross-platform) or `security-framework` (Mac-specific) for Keychain access? Decide in implementation; both work.
- **D2**: How does the user revoke their own device from the in-app UI? Defer to a follow-up settings panel; first version only supports backend-side revocation (e.g., on org removal).
- **D3**: Do we ship camera/location/canvas plugins in v0.3.0 or hold them for v0.4? Hold. Migration ships v0.3 with feature parity; new capabilities are a separate scoped effort.
- **D4**: Tauri 2 vs Tauri 3 timing — we're on Tauri 2 today; migration doesn't force a Tauri upgrade. Note for later.

## Success criteria

- `browser.proxy` works end-to-end against dev for a Resy login flow
- `system.notify` UX matches v0.2.x (rich, native macOS notification)
- `device.permissions` accurately reports TCC grants for screen-recording, accessibility, etc.
- `system.run` calls receive approval prompts the same way they do today
- All 16 current commands have a clear owner (subprocess or Tauri-mac) and round-trip successfully
- node.list shows two entries per user device with distinguishable displayNames
- Backend integration tests pass; CI contract test green against latest openclaw upstream
- Bundle DMG <= 450 MB
- Subprocess restart time <= 3s on JWT rotation (no user-visible chat interruption beyond a brief reconnect indicator)
