# Machine relay off the API Gateway WebSocket door — design (sub-project D)

Status: written 2026-09-12 from the code (every claim checked); **owner decisions still
open are listed in §2** — the recommended approach is written out in full so the plan can
be reviewed alongside, but D does not execute until the owner confirms §2 and A and B have
shipped. Fourth sub-project of the durable-architecture stream (A async turn contract,
B stateless backend, C lifecycle as Step Functions). D consumes B's `container_rpc.call`
and B's `agent_end` usage hook, and it is what lets `desiredCount` in prod leave 1.

## 1. Problem

Three machine clients reach the owner's container through `wss://ws[-env].flickmeon.ai`,
an API Gateway v2 WebSocket API whose `$connect` / `$default` / `$disconnect` are Lambdas
that POST each frame to `routers/websocket_chat.py` over the ALB. API Gateway delivers each
frame as an independent HTTP request that **any** backend instance may receive:

| client | `clientId` | what the backend does today | per-instance state |
|---|---|---|---|
| Tauri desktop node host (two OpenClaw nodes per Mac: the CLI subprocess and the mac companion) | `node-host`, `openclaw-macos` | `node_proxy.py`: opens an upstream WS to the container (`core/gateway/node_connection.py`, a pure protocol relay of OpenClaw's own signed-device handshake), relays frames verbatim, self-heals `NOT_PAIRED`, approves the node command surface, flips the `tools.deny` list when the first node arrives / last node leaves, clears `execNode` on sessions at disconnect | `_node_upstreams`, `_user_nodes`, `_node_count`, `_patched_sessions` |
| Paperclip `openclaw_gateway` adapter | `paperclip-operator` | `operator_proxy.py`: the same relay class (the adapter runs the real device handshake with its own `devicePrivateKeyPem`), plus a wake of a stopped container, a trial-expiry disarm of the nightly routine, the trial-cap wake gate on `chat.send`/`agent`/`sessions.*`, and a run-completion telemetry tap | `_operator_upstreams`, `_run_telemetry_tasks` |
| Desktop local-session relay | `lightbulb-local-sessions` | `local_session_relay.py`: **already stateless** ("DynamoDB fences every worker and reconnect") and it *needs* API Gateway's Management API: `routers/messaging.py` dispatches room work to a device from whichever instance handled the HTTP command | none |

With two instances, a node's or operator's frame lands on the instance that does not hold
its upstream socket and the client gets "relay unavailable on this node — reconnect"
(`websocket_chat.py:_send_relay_unavailable`). That is the last reason `service-stack.ts`
pins `desiredCount: 1` after B.

## 2. Decisions for the owner

1. **Approach (recommended: A below).** Move node hosts and Paperclip operators to a
   raw WebSocket door where one backend task owns both sockets for the connection's life
   — the pattern `routers/browser_view.py` already runs on `wss://bv[-env].flickmeon.ai`.
   The local-session relay stays on API Gateway (it is the one client that genuinely needs
   the Management API). Alternatives considered and rejected:
   - *Keep API Gateway and route frames to the holding instance* — needs an instance
     registry and inter-instance forwarding: a second bus.
   - *Expose the container gateway directly* (per-owner public hostname to the task) —
     per-owner edge routing rules, a public auth surface on every container, Cloud Map IP
     churn on every deploy.
   - *Delete the WebSocket API entirely* — blocked by the local-session relay's Management
     API pushes; API Gateway is the right tool for that one client.
2. **Device revocation lag.** Today `DELETE /desktop/devices/{id}` force-closes the live
   socket through the in-process map. With instance-pinned sockets, revoke deletes the
   device's presence row and the relay notices within its 60 s refresh and closes. The
   device JWT itself lives ≤ 300 s and `paired.json` removal blocks re-auth, so the window
   is bounded. Recommended: accept the ≤ 60 s lag.
3. **Prod instance count.** D's cutover sets `desiredCount: 2` in prod with a CPU-based
   scalable target (2–4, 60 %). Recommended as written; the owner may prefer a fixed 2.
4. **Paperclip adapter URL backfill.** Every provisioned agent's `adapterConfig.url` names
   the old host. D ships a one-shot re-runnable script (dry-run default, `--apply`) that
   rewrites it per company; deleted after both environments ran it.

## 3. Principles

Inherited: any instance serves any request; no clocks for coordination; delete, do not
port; fix forward. Added: **a socket the backend must hold is pinned to the instance that
accepted it and carries no cross-request state** — everything another instance may need
(presence, counts) is a DynamoDB row with a TTL, and every per-connection side effect runs
inside the handler that owns the socket.

## 4. Approach A: the gateway relay

### Door

`wss://bv[-env].flickmeon.ai/api/v1/gateway/ws?token=<jwt>&clientId=<kind>` — the
existing CloudFront distribution (`BrowserViewDistribution`, `ALL_VIEWER` origin request
policy, caching disabled) and ALB listener `:8081`; the listener rule's path cap grows
from `["/api/v1/browser-view/*"]` to `["/api/v1/browser-view/*", "/api/v1/gateway/*"]`.
Both clients already put the credential and the kind in the query string (desktop
`proxy.rs:166`, `paperclip_adapter_config.synthesize_openclaw_adapter`), so their wire
shape does not change — only the host and path.

### Handler (`routers/gateway_relay.py`, mounted at `/api/v1/gateway`, `@router.websocket("/ws")` stacked with `/ws/` on one handler — D2)

1. **Authenticate the upgrade** with `core/services/machine_auth.py`, a port of the two
   verifiers in `apps/infra/lambda/websocket-authorizer/index.py`: device JWTs (EdDSA,
   `kind=device`, ≤ 300 s lifetime, signature-bound public key from the owner's container
   row) and Paperclip service tokens (HS256, `kind=paperclip_service`, `service_token`'s
   signing key). `clientId` admission is the Lambda's: `node-host` / `openclaw-macos`
   need a device token; `paperclip-operator` needs a service token; anything else closes
   with 1008. Owner and user come from the token, never from the client.
2. **Resolve the container** with `get_ecs_manager().resolve_running_container(owner_id)`.
   No running container: `request_wake_silent(owner_id, door="relay")` (it does all the
   gating: trial cap/expiry, entitlement, operator stop), the operator path additionally
   runs `_maybe_disarm_expired_trial` (moved verbatim from `operator_proxy`), then close
   with 1013 (try again later); both clients reconnect on their own loop.
3. **Write the presence row** in `ws-connections`: `connectionId = "relay#<uuid>"`,
   `connectionType` `node` | `operator`, `userId`, `orgId`, `deviceId`, `clientId`,
   `expiresAt = now + 120 s`; a task refreshes `expiresAt` every 60 s with a conditional
   update (`attribute_exists(connectionId)`); the row is deleted on close. A refresh that
   finds the row gone (revoke) closes the socket with 1008. `has_live_operator_connection`
   is unchanged (it counts `operator` rows); node presence is `query_by_owner` filtered to
   `node`.
4. **Pipe** `ws://<ip>:18789` ↔ client, text and binary frames verbatim, both directions,
   `max_size` as `browser_view.py`, close propagated either way. The OpenClaw handshake
   (`connect.challenge` → signed `connect` → `hello-ok`) is the container's and the
   client's business; the relay never rewrites a frame or a request id.
5. **Two frame hooks, the only parsing the relay does** (text frames, `json.loads` on
   frames under 64 KiB, everything else passes untouched):
   - client → container, `type == "req"` and `method ∈ {"chat.send", "sessions.send",
     "sessions.steer", "agent"}` on an operator connection: `chat_gate.gate_chat`; a
     blocked gate answers the client with today's refusal payload and drops the frame
     (`websocket_chat._admit_operator_wake` / `_wake_refusal_payload`, moved verbatim).
   - container → client, the first `res` with `error.code == "NOT_PAIRED"`: node →
     `heal_desktop_device_not_paired(owner_id, device_id, request_id)`; operator →
     `approve_pending_device_from_rejection(owner_id, device_id, request_id)` (both are
     B-era functions on `container_rpc`); the client's own reconnect then lands paired.
     This replaces `paperclip_openclaw_pairing._probe_pairing_request` and
     `perform_signed_connect`: agent creation no longer dials the container as the agent's
     device; the adapter's first connect opens the pairing window and the relay closes it.
     D15: the self-heal is gated on the deviceId already being in the owner's
     backend-written `devices/paired.json` (a compromised/buggy container's own NOT_PAIRED
     frame is otherwise the only source of that deviceId) and fires once per connection,
     not on every retry.
6. **Node side effects** (unchanged behaviour, new home): after `hello-ok` on a node
   connection, `approve_pending_node_surface(owner_id, device_id)` and, when this is the
   owner's first live node row, `patch_openclaw_config(owner_id, {"tools": {"deny":
   TOOL_DENY_BASE}})`; on close, when no node row remains, the `TOOL_DENY_WITH_NODES`
   patch back; `runtime.resource.invalidated {resource: "health", resourceId:
   "desktop-node"}` published on both edges as today. `_patched_sessions` /
   `clear_patched_sessions_for_user` are deleted: nothing ever populated them
   (`set_patched_session` has no caller), so the `sessions.patch execNode` clear at
   disconnect was dead.
7. **Operator side effects**: the run-completion telemetry tap is deleted (B's `agent_end`
   hook reports every run).

### Removed

`routers/websocket_chat.py`'s node and operator branches (the router keeps `/ws/connect`,
`/ws/disconnect`, `/ws/message` for `lightbulb-local-sessions` only), `core/services/
node_proxy.py`, `core/services/operator_proxy.py`, `core/gateway/node_connection.py`,
`core/gateway/operator_rpc.py` (whole file: `perform_signed_connect` and its helpers;
`_probe_pairing_request` and the probe step of `ensure_operator_device_paired` go with
it — the function keeps staging `paired.json` and the adapter identity), `force_close_device`
(revoke deletes presence rows instead), the authorizer Lambda's Paperclip service-token
path and its `node-host` / `openclaw-macos` / `paperclip-operator` admissions
(`ws-connect` admits `lightbulb-local-sessions` only), and their tests. CLAUDE.md's
WebSocket Architecture items 1–3 are rewritten for the local-session-only door and item
5 (the raw `bv-` door) gains the relay path; the `service-stack.ts` row loses its
"NOT autoscaled" paragraph.

### Kept

The WebSocket API, its three Lambdas, the authorizer (device path), the `ws-connections`
table, `ManagementApiClient`, `local_session_relay.py`, P10 and W30–W32, `WS_GATEWAY_URL`
for the local-session enrollment URL (`transport.rs` reads `enrollment.gateway_url`).

## 5. Configuration and clients

- Backend setting `GATEWAY_RELAY_URL` (CDK: `wss://${browserViewDomain}/api/v1/gateway/ws`,
  exported next to `browser-view-url`), consumed by `POST /desktop/pair` (`gateway_ws_url`),
  `paperclip_provisioning._ws_gateway_url`, `company_deploy_service._gateway_url`, and
  `paperclip_adapter_config.build_gateway_url_re` (allowlist becomes the `bv` host).
- Desktop: `LIGHTBULB_WS_URL` in `desktop-build.yml` (dev and prod jobs) → the relay URL;
  `lib.rs` defaults likewise. D1: `POST /desktop/pair`'s response gains `relayUrl`
  (`settings.GATEWAY_RELAY_URL`) alongside the existing `gatewayUrl` (stays
  `WS_GATEWAY_URL`, still read by the local-session relay only); `bootstrap.rs` reads
  `relay_url` and dials it for the node's own proxy, so the node client is not
  code-change-free after all.
- Paperclip: the backfill script of §2.4 rewrites `adapterConfig.url` for every
  `openclaw_gateway` agent through the Paperclip admin API.
- `service-stack.ts`: `desiredCount: 2`, `minHealthyPercent: 100`, `maxHealthyPercent:
  200`, a scalable target 2–4 on CPU 60 %; the comment that explains why it was 1 is
  deleted. `security-group` and `ContainerFromServiceIngress` are unchanged (the backend
  already dials the container on 18789).
- D16: uvicorn's own WS handshake log (`uvicorn.error`, independent of any app-level
  logging) writes the upgrade request line including `?token=<jwt>` in plaintext.
  `core/observability/logging.py`'s `configure_logging()` — called from `main.py` at
  import — installs an idempotent `RedactTokenFilter` on that logger so the credential
  never reaches CloudWatch.

## 6. Failure handling

| Failure | Detected by | Outcome |
|---|---|---|
| bad or expired token, unknown `clientId` | upgrade auth | close 1008; the client re-mints and reconnects |
| container stopped | `resolve_running_container` | D14: operator connects wake it (`request_wake_silent`), close 1013, reconnect loop lands once it is up; a node connect never wakes (`outcome=skipped_node`) — an old node path never woke either, and waking on a laptop coming back from sleep would re-provision a parked trial container forever |
| container closes / task dies mid-connection | upstream EOF | client socket closed with the same code; the client reconnects and the next accept resolves the new IP |
| backend deploy | instance drains | both sockets close; the client reconnects to a surviving instance (`minHealthyPercent: 100`) |
| device revoked while connected | presence row gone at refresh | close 1008 within 60 s; re-auth fails (`paired.json`, token expiry) |
| presence write fails | logged | the connection proceeds; trial idle may stop a container under a Paperclip run (as today when a row write failed); node deny-list patch may be skipped until the next connect |
| gate read fails | `chat_gate` exception | frame forwarded (Paperclip work must not be lost; the Bedrock gate remains the fence), `wake.admission {outcome: gate_error}` |

## 7. Rollout

1. Backend PR: `machine_auth`, `gateway_relay`, presence rows, hooks moved, deletions,
   settings, infra (listener rule, CloudFront behaviour unchanged, export, `desiredCount`
   + scaling), authorizer/`ws-connect` narrowing, `desktop-build.yml`, backfill script,
   CLAUDE.md. Dev deploy on merge.
2. Dev: run the backfill (`--apply`) for dev companies; build a dev desktop from the branch
   (`desktop-build.yml` dev job) and pair it; verify §8.
3. Prod (owner-gated): dispatch, run the backfill on prod, promote the desktop build.
   Old desktop builds keep dialing `wss://ws.…` and get a clean 403 from the narrowed
   authorizer until updated (no compatibility path; the updater ships the new build).
4. Delete the backfill script and this spec/plan.

## 8. Testing

- Unit: `machine_auth` (both verifiers, lifetime ceiling, wrong kind per `clientId`);
  the relay handler with a fake upstream (auth reject codes, wake path, presence row
  lifecycle incl. revoke-at-refresh, verbatim piping of text/binary, gate refusal frame,
  NOT_PAIRED hook per kind, node deny-list edges at first/last row); the backfill script
  dry-run vs apply.
- Dev, at `desiredCount: 2`: a Mac pairs and its two nodes reach `hello-ok` through both
  instances (ALB target request counts); an `exec` on the node path works; a Paperclip
  heartbeat run completes end to end and its usage lands once; revoke closes a live node
  within 60 s; a rolling deploy during a Paperclip run: the adapter reconnects and the
  run completes or is reported, never lost silently; trial idle does not stop a container
  under an operator connection.
- Instruments: `relay.connect {kind, outcome}`, `relay.close {kind, code}`,
  `wake.admission` (kept), `relay.presence {outcome}`.

## 9. Success criteria

- `desiredCount: 2` in prod for a week with no "relay unavailable" and no relay-attributed
  incident; `git grep node_proxy operator_proxy node_connection operator_rpc` empty.

## 10. Out of scope

The local-session relay's transport (stays); per-owner rate limits on the relay;
replacing the `bv-` CloudFront door with something else.

Handled by C's PR, not D's: `idempotency.py` (replaced by a 409 dedup-table claim);
the P12 backend-liveness alarm; the `run_completed` lifecycle tracking. The Task 4b
amendment (a DDB `connection_lock` lease) was withdrawn — C's spec analysis found the
existing `owner_lifecycle_guard` (EFS fcntl) is already cross-instance, so C reuses it
instead of adding a second lock.
