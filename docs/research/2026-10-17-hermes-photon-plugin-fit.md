# Hermes Photon plugin: fit for our AgentCore runtimes (2026-10-17)

Sources: NousResearch/hermes-agent `main`, `plugins/platforms/photon/` (plugin.yaml v0.3.0,
adapter.py 1739 lines, sidecar/index.mjs 1294 lines, sidecar/package.json v0.4.0) and the
npm tarballs `spectrum-ts@12.7.0`, `@spectrum-ts/imessage@12.7.0`, `@spectrum-ts/core@12.7.0`
(the version the sidecar pins). Our side: this worktree.

## Verdicts

- Q1 RECEIVE: CONFIRMED. Inbound is a long-lived gRPC subscription only; no webhook or pull mode exists in the plugin.
- Q2 SEND: PARTLY CONFIRMED. The sidecar's `/send` is `space.send()`, a unary call on a per-line gRPC client built at `Spectrum()` init; in SDK code it does not read the `app.messages` loop. Send without a subscription is UNVERIFIED (not run).
- Q3 ONE CLIENT, MANY LINES: CONFIRMED in SDK code (dedicated mode builds one gRPC client per line, routed by `phone`). Live behaviour against Photon is UNVERIFIED.
- Q4 OUR RUNTIME: CONFIRMED. The receive half cannot run in a runtime that lives per invoke and parks after 30 min idle. Whether our base image ships the plugin is UNVERIFIED.
- Q5 OUTSIDE THE RUNTIME: Feasible, UNVERIFIED end to end. Reuse about half the sidecar, or write 150-250 lines against `spectrum-ts`. Whether our Photon 2.0 `pho_sk_` key works with the SDK is UNVERIFIED.

## Q1. Receive

plugin.yaml lines 5-12: "Both directions run over the `spectrum-ts` SDK's long-lived gRPC
stream via a small supervised Node sidecar - inbound messages arrive on the SDK's
`app.messages` stream (no webhook, no public URL, no signing secret)".

sidecar/index.mjs:
- lines 8-13: "the SDK's `app.messages` async iterator is a long-lived gRPC stream ... stream it to the Python adapter over a loopback `GET /inbound` (NDJSON). We pause pulling from the stream while no consumer is attached".
- line 332: `const app = await Spectrum({ projectId, projectSecret, providers: [imessage.config()], options: { flattenGroups: true }, telemetry })`.
- line 701: `for await (const [space, message] of app.messages) {` inside a `for (;;)` re-subscribe loop (697-728).
- line 1029: `GET /inbound`; server binds `PHOTON_SIDECAR_BIND`, default `127.0.0.1` (line 80).

adapter.py: `_inbound_loop` reads `/inbound` (725-751); `_dispatch_inbound` (817).
`class PhotonAdapter(BasePlatformAdapter)` at 559 imports `gateway.config` and
`gateway.platforms.base` (37-47). Registered by `ctx.register_platform(...)` at 1702
(`kind: platform` in plugin.yaml). `connect()` (647) spawns the sidecar with
`subprocess.Popen` (1053; `PHOTON_SIDECAR_AUTOSTART`, line 577). So it runs only when the
Hermes gateway process loads platform adapters. No webhook or polling mode exists.

## Q2. Send

sidecar/index.mjs 1067-1089:
`if (req.url === "/send") { const { spaceId, text, format = "text" } = body || {}; ... const space = await resolveSpace(spaceId); ... const result = await space.send(builder);`
`resolveSpace` (962-1002) uses `im.space.create(phone)` for a bare E.164 number and
`im.space.get(id)` for a group GUID. sidecar/README.md says the sidecar exists because "Photon
has no public HTTP send-message endpoint" and will be retired when one ships.

SDK, `@spectrum-ts/imessage` dist/index.js, line 2966-2967:
`const remote = clientForPhone(client, space.phone); return cacheRemoteOutbound(remote, space, await send(remote, space.id, content));`
The gRPC clients are created in `createClient` (2846-2861) at `Spectrum()` init. Inbound uses
other calls (`client.messages.subscribeEvents()`, `client.events.catchUp(since)`, 2428-2462).
By the code, send is its own RPC on the same client object, not a side effect of the
subscription, so a short `node send.js` would build a client, send, close. UNVERIFIED: it was
not run, and `Spectrum()` init calls `cloud.issueImessageTokens` (line 432) once per process.
Photon policy per the plugin README (~line 181): shared lines cannot start a conversation with
a number that never texted the line.

## Q3. One client, many lines

SDK dist/index.js:
- 431-535 `createCloudClients`: `issueImessageTokens(projectId, projectSecret)`. `type === "shared"` gives one client to `imessage.spectrum.photon.codes:443`. Otherwise dedicated: one `createGrpcClient` per line at `address: \`${instanceId}.imsg.photon.codes:443\`` with a per-line token, lines added and retired as the project's token set changes (`removeMissing`, `retire`).
- ~2878 `space.create`: `const phone = input.params?.phone ?? randomPhone(client);` `space.get` throws "requires params.phone when multiple clients are configured". Spaces carry `phone`; sends route by `clientForPhone(client, space.phone)`.
- sidecar index.mjs 653 forwards `phone: space.phone ?? msgSpace.phone` on every inbound event, so inbound is tagged with the receiving line.
Gap in the Hermes sidecar: `resolveSpace` calls `im.space.create(phoneTarget)` with no `phone` param (line 977), so a new outbound DM in dedicated mode picks a random line. Our service must pass `params.phone`.

## Q4. Our runtime

- `apps/infra/hermes/Dockerfile` ends `CMD ["python3", "-m", "bridge.server"]`.
- `bridge/server.py` line 3: "AgentCore speaks to this container on :8080 (GET /ping, POST /invocations)"; line 819 `ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()`; only `/invocations` is served (755).
- grep for `gateway run|GatewayRunner|start_gateway|platforms` in `bridge/server.py`, `bridge/__init__.py`, the Dockerfile and `managed/config.yaml` finds nothing that starts the Hermes gateway. `bridge/turn.py:2455` mirrors adapter code because importing `gateway.run` is avoided. "photon" appears nowhere under `apps/infra/hermes`; only in `apps/backend/core/config.py:230-233` (our Photon 2.0 key, project id, webhook secret).
- Lifecycle: `agentcore_runtime.py:117` `IDLE_TIMEOUT_S = 30 * 60`; `:119` `MAX_LIFETIME_S = 8 * 60 * 60`; `hermes_provisioning.py:202-205` sends them as `idleRuntimeSessionTimeout` / `maxLifetime`. CLAUDE.md: "AgentCore starts the microVM on the invoke itself"; "AWS parks an idle microVM invisibly"; the bridge "saves the profile back at the END of the turn". No CLAUDE.md sentence says a park kills every process; that is AgentCore microVM behaviour, UNVERIFIED from our code, but nothing in the bridge restarts a child after a wake.
- Cannot run in the microVM: the gateway platform loop, the `/inbound` consumer, the sidecar's `app.messages` subscription. Each needs a process alive between invokes. A message that arrives while parked is lost or replayed only by SDK catch-up (UNVERIFIED).
- Could run: an on-demand `space.send()` inside a turn, if Q2 holds. It needs Node in the image (not checked) and the project secret in the runtime, which we keep out of runtimes.
- Base image: `hermes-version.json` pins `nousresearch/hermes-agent@sha256:a5b2eac6...`, version "0.21.3", pulled 2026-09-16. The plugin's first commit on main is 2026-06-08, so it is probably in that digest. Sidecar `node_modules` come from `hermes photon setup`, so it would not be ready to run anyway. UNVERIFIED: the image was not extracted.

## Q5. One always-on service we own

- Needs (index.mjs 77-81): `PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET`, port, bind, `PHOTON_SIDECAR_TOKEN`. Our config holds `pho_sk_` / `pho_prj_` (Photon 2.0); that these equal the SDK's `projectId` / `projectSecret` is UNVERIFIED.
- Binding: one project, one `Spectrum()` client, N lines (Q3). Inbound carries `space.phone`; the backend maps line to workspace.
- Reconnect (index.mjs): re-subscribe loop with backoff 1 s doubling to 30 s plus jitter (693-728); zombie watchdog and silence probe (736-800, `stream-staleness.mjs`, 90 s degraded restart); `app.stop()` on SIGINT/SIGTERM (1261); stdin-EOF self-exit (1280). Delivery waits for a consumer (`deliver`, 418). We would swap that consumer for an HTTP POST to our backend and add our own ack and retry.
- "7 overrides": `sidecar/package.json` `overrides` lists exactly seven pins: `undici`, `protobufjs` and five `@opentelemetry/*` packages. They are dependency pins, not SDK behaviour patches. The real risk is the `postinstall` script `patch-spectrum-mixed-attachments.mjs` (188 lines): it string-patches `@spectrum-ts/imessage/dist/index.js` and fails loudly if the mapper changes. Text-only flows do not need it; we could skip it.
- Reuse: of 1294 sidecar lines, about 450-600 (stream loop, watchdog, `resolveSpace`, send and typing handlers, shutdown) carry over. About 400-500 (NDJSON consumer, Hermes event shape, read receipts, attachment inlining) get rewritten. Reactions, polls, effects and richlinks can wait. A fresh file against `spectrum-ts` is 150-250 lines. The Python adapter (1739 lines) is not reusable: it is built on `gateway.*`.
- Stable API: this gives SDK send and receive end to end. It does not show instant line creation; line allocation is on Photon's REST side, not in this plugin. UNVERIFIED which of Photon's APIs our notes call "stable".

## What this means

- The claim holds for receive: the plugin is a gateway platform adapter plus a Node child process holding a gRPC stream.
- Nothing in our bridge starts the Hermes gateway, so the plugin would not load there.
- The SDK is multi-line (per-line gRPC clients, `params.phone`), so one always-on service can serve a whole project.
- Cheapest fit: one ECS task running a small `spectrum-ts` service of our own that POSTs inbound to the backend.
- Send may work on demand, but test it first; it needs the project secret outside our runtimes.
- Before building: check that our `pho_sk_` credentials work with `Spectrum()`, and run a send-without-subscribe test.
