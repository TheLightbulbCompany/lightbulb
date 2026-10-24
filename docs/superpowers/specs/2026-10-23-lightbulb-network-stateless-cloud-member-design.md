# Lightbulb Connect: the workspace member inside the AgentCore runtime (design)

Status: revision 3.1. It replaces the stateful "bridge" in Plan C. Nothing here is built. The
Plan C backend code (`routers/network.py`, `network_issuer.py`, `apps/bridge/`, `relay-stack.ts`,
`network-version.json`, the `/connect/*` rewrites, the `NETWORKSPACE`/`NETLINK` rows) was reverted
in #4304. Every one of those is **to build**. Revision 3.1 applies the rulings on
`.superpowers/sdd/charter-c-design-rev3-builder-check.md`; the index at the end maps each item.

> **Owner-approved shape, 2026-10-24.** The workspace is one more computer on the free
> `lightbulb` network. Its computer is the engine, running as a sidecar inside the workspace's
> AgentCore microVM, with its own identity key (in the owner's boot secret) and its state in the
> owner's S3 prefix (`<owner>/network/`). It founds the workspace's team. Friday and the other
> cloud agents are that computer's declared sessions. Everything else is the engine's existing
> roster, holds, receipts, relays and tools, unchanged.

Words: a **network message** is the engine's store-and-forward message between members (the
engine's code module is `mail.rs`). This document never means email.

Sources read:
- the engine at `lightbulb-network.nosync` `main` (v0.2.4): `src/embed.rs`, `src/daemon/{ipc,net_ops}.rs`,
  `src/net/mod.rs`, `src/{mail,outbox,policy,holds,ticket,admission,roster,identity,os,dialog,presence,paths,cli}.rs`;
- the Hermes image: `apps/infra/hermes/Dockerfile`, `bridge/{__init__,server,sync,recycle,turn}.py`,
  `plugins/lightbulb/__init__.py`;
- the backend: `core/services/{openclaw_work,agentcore_runtime,hermes_provisioning,runtime_transport,webhook_agents}.py`,
  `core/repositories/messaging_repo.py`;
- infra: `container-stack.ts`, `network-stack.ts`, `api-stack.ts`, `lambda/messaging-projector/index.py`.

Claims I could not check in code are marked **(unverified)**.

---

## 1. Intent and why

Plan C ran the member as an always-on Fargate task holding every workspace's key, with S3
snapshots and a DynamoDB lease. The reviews found that the lease and the snapshots race (Task 5
HIGH-1 and HIGH-2).

The owner's rule: stateless, cheaper, on AWS. Every workspace already has one stateful,
single-writer, S3-backed, wake-on-invoke compute unit: its AgentCore runtime. The member moves in.

## 2. What must exist in the cloud at all

- **The workspace's computer.** The agents live in the cloud, so their network messages are
  received there. It holds the member key, the outbox and the seen-ids. It founds the team.
- **Connecting a Mac.** Settings shows "Connect your computer" with the engine's installer and a
  `lightbulb join '<ticket>' --name <you>` line the member minted for that person. They paste it
  in a terminal. No device pairing, no desktop app, no new auth.
- **Hosted assistants** (ChatGPT, Claude.ai, Grok Bot, through the hosted MCP at the backend)
  send through the member, labelled "<App> via <Person>", whether or not the person's Mac is on.

There is no channel from the backend to anyone's Mac. The relay is n0's public relays.

## 3. What goes through the backend, and what never does

| Through the backend | Never through the backend |
|---|---|
| Wake signals, which carry no content (the doorbell, 4.6, if allowed) | Network message bodies between members |
| Runtime invocations (`InvokeAgentRuntime` envelopes with no network message content) | Hold text (hold notices carry ids, the sender label and the target only) |
| The invite line: a credential for that person, shown once | The roster and the engine state |
| A hosted app's requests, carrying only the text that hosted app itself sent | |
| Member hold decisions: hold id, verdict, scope and `decided_by` | Streams (not in phase 1) |
| The member key, once: minted into the boot secret (4.3) | |

- **Inbound network messages to Friday run as local turns** (job 2). They are not posted into a
  room. Friday decides what to say, where, and to whom. Rooms and the network stay disconnected.
- **The engine's state sits at rest in the owner's S3 prefix.** Backend code never reads it. The
  owner accepted this.

## 4. The design

### 4.1 Components

| Piece | Where | Status |
|---|---|---|
| Engine Linux arm64 build | engine repo CI, Hermes image build stage | to build (task 1) |
| `lightbulb member`, the sidecar | baked into the Hermes image, started by the bridge | binary `lightbulb` exists; subcommand to build (E1) |
| `<owner>/network/state.db` | `OWNER_STATE_BUCKET` | new object; existing bucket and `<owner>/*` grant |
| `<owner>/network/sessions.json` | written by the backend, read by the bridge | to build |
| Member key `networkKey` | the owner's boot secret | new field, existing secret |
| `bridge/network.py` | the Python bridge | to build |
| Friday's network tool | `plugins/lightbulb/`, calling the sidecar socket in process | to build |
| "Connect your computer" panel and its invite route | Settings (web), backend | to build |
| Doorbell route and `network_wake` `WORK#` row | backend → messaging table → Pipe → `openclaw-work.fifo` | to build, if allowed (4.6) |
| Member holds as Needs-you items | the webhook-agent hold rows and renderer | reuse |
| Relay | n0's public relays | existing |

There is no Lambda, no lease, no VPC-only listener, no always-on process, no state route, and
nothing new in the desktop app.

### 4.2 Verified facts this builds on

Runtime:
- **Warm pool.** "Nothing session-bound happens before the first /invocations: AgentCore's warm
  pool runs this CMD on ~10 pooled instances per runtime version" (`server.py:13-14`).
  `bridge/__init__.py:56-64` reads the boot secret once, at import, on pooled instances too.
- **One session per owner.** The session id comes from the owner id and a cold start is
  single-flight (`runtime_transport.py`; CLAUDE.md).
- **Timings.** `IDLE_TIMEOUT_S = 30*60`, `MAX_LIFETIME_S = 8*60*60` (`agentcore_runtime.py:117-119`).
  The bridge recycles at 7 h (`recycle.py`). The caller's budget is 90 s (`runtime_transport.py:41`).
  A cold workspace measured 54–57 s (CLAUDE.md).
- **Stop.** On SIGTERM AWS gives ≤ 15 s, and the bridge saves every restored profile within
  `SIGTERM_SAVE_S = 10.0`, then `os._exit` (`server.py:781-803`).
- **The boot secret.** `write_boot_secret` reads the prior secret and writes exactly
  `{hookToken, gatewayToken, bedrockBearer}`, dropping any other key (`agentcore_runtime.py:167-191`).
  `prepare_hermes_workspace` calls it on every reconcile (`hermes_provisioning.py:653-713`). The
  per-owner role has only `GetSecretValue` on its own boot secret (`container-stack.ts:139,196`).
  The agents' shells can read the execution role's credentials (comment in `bridge/__init__.py`).
- **Conditional puts** (`IfMatch`) already exist (`sync.py:1159,1210-1217`).
- **Egress.** The runtime security group allows all outbound; the VPC has one NAT gateway.
- **Run-less turns.** With no `sessionKey`, a turn gets a new session per event
  (`turn.py` `_resolve_session_key`). In a run-less turn, `send_room_message` builds its
  `eventId` from `ctx['runId']` (`None`) and a per-process counter (`plugins/lightbulb/__init__.py:79-84`).
- **Webhook agents** have no profile. Their URL and key are one Secrets Manager secret, and the
  backend posts (`webhook_agents.py:1-12`).

Work path:
- The Pipe forwards only messaging-table inserts whose `sk` begins `WORK#`, with the row's
  `messageGroupId` and `messageDeduplicationId` (`api-stack.ts:1025-1029,1051-1055`).
- `deserialize_pointer` reads `NewImage.payload.M` and refuses unknown kinds (`openclaw_work.py:104-155`).
- The projector ignores every `sk` that is not `MSG#` or `LIVE#` (`messaging-projector/index.py:1124`).

Engine (v0.2.4):
- The engine is built and tested only on macOS (`ci.yml:8`, `release.yml`; `install.sh` refuses
  anything but Darwin). No Linux binary is published.
- `embed::start` requires `kind: Cloud` and opens no socket. `EmbedOpts` is `{kind, relay,
  snapshot, name}`; there is no issuer. `Instance::shutdown` closes the endpoint, waits ≤ 5 s,
  then writes the final snapshot outside the home (`embed.rs`).
- On first start, `boot` writes a solo genesis whose member is `kind: "computer"`, role
  `founder`, no proof (`net_ops.rs:386,395`). So the workspace founds as an ordinary computer.
- `boot` refuses with `KEY_MISMATCH` when `identity.key` does not match a kept `state.db`
  (`embed.rs:78,130`). The engine loads its key only from `identity.key` (`identity.rs:29-44`).
- v0.2.4 has no `http.rs`, no relay-list fetch and no proof renewal. The `update` command shells
  out to the system `curl` (`cli.rs:1148`).
- `Instance::call` runs ops as `Caller::Instance`. `NOT_ON_AN_INSTANCE` refuses `team.invite`,
  `reload`, `ui.claim` and `shutdown` (`ipc.rs:166`). An instance is not a person
  (`Caller::is_person`), so person-only ops, every hold op among them, refuse it.
- Admission: `ticket.mode != "open" || kind == "cloud" || same_person` waits for an Allow
  (`admission.rs:311-314`). Once links wait too; only `open` links skip it.
- `policy::decide` holds a first message from another person unless a PEOPLE row says otherwise
  (`policy.rs:123-140`). The member, a `computer`, gets `decide`, not `cloud_inbound`.
- `mail::waiting_keys` lists every key with waiting messages; `mail::waiting` reads without
  taking; `take_one` moves a receipt to `delivered`; `mark_read(store, id)` to `read` (`mail.rs:886-970`).
- `mail::forget` drops a message's text once its receipt is final (`read`, `refused`, `expired`)
  and nothing waits on it; the row stays (`mail.rs:980-985`).
- The `message` event rides a 256-slot broadcast and can be missed (`embed.rs`; `ipc.rs:686`).
- On start the engine dials every active member at once, then every 30 s (`net/mod.rs`, `keep_up`).
  A sender flushes its whole outbox on `hello`. Queued messages wait in the **sender's** outbox,
  retried only to online members, for `TTL_S = 86_400` (`outbox.rs`).
- A cloud instance refuses a network message for an undeclared session (`mail.rs:762`).
  `cloud.sessions.set` takes `{sessions: [{name, agent, title?}]}`, at most `presence::MAX = 100`.
- One key keeps one live link per peer: a newer connection closes the older with `SUPERSEDED`
  (`net/mod.rs:465-482`). The founder cannot `team.leave` (`FOUNDER_MUST_TRANSFER`).
- A hold on a Mac is answered in an `osascript` dialog (`dialog.rs`). `hold.answer` records
  `by: "local"`; so do two other paths (`holds.rs:543,623,667`).
- `Ticket` gained `addrs` with `#[serde(default)]` and no `deny_unknown_fields` (`ticket.rs:28-33`),
  so new optional fields need no protocol bump.

### 4.3 The member key

- **Minted once by the backend** into the owner's boot secret as `networkKey` (a base64 Ed25519
  seed), when the workspace first connects.
- **Kept across reconciles:** `write_boot_secret` already reads the prior secret; one line in its
  key set carries `networkKey` over. No other backend code reads it.
- **On the microVM:** at engine start the bridge writes it as the sidecar's `identity.key` (mode
  0600) in the engine home. It is never uploaded: only `state.db` goes to S3.
- **A warm runtime** read its boot secret at import, so connect stops the session
  (`stop_runtime_session`) after minting; the next invoke cold-starts with the key.
- **Reprovision:** the seam that drops the boot secret also deletes `<owner>/network/`, or
  `KEY_MISMATCH` blocks every later start. The workspace shows "Reconnect workspace" (a new team).
- **Rotation** (a new key, `--new-key`) is phase 3.

### 4.4 Bridge ↔ engine contract

The sidecar (E1) is `embed::start` plus a Unix socket. Every line goes to `Instance::call` as
`Caller::Instance`. Each line carries the per-boot control token, held only in bridge memory, so
an agent's shell cannot use the socket.

**Start trigger.** Only inside `handle_plugin`, on a real `/invocations`, when `networkKey` is
in the boot secret and `sessions.json` exists. A pooled microVM gets no `/invocations` until it is
claimed, and one microVM per owner is claimed, so two never start an engine with one key.
`ensure_started()` runs under one lock; a `/network/*` path waits for it (about 1 s
**(unverified)**), any other path starts it in the background. A failed start logs
`network.start_failed` and retries after 60 s. It never starts a second sidecar.

**Start:** get `state.db` (keep its ETag, or "absent") and `sessions.json`; write `identity.key`;
spawn `lightbulb member --home /opt/data/network --snapshot /opt/data/network-out/state.db
--socket /opt/data/network.sock --name <ws name> --sessions <file>`, token on stdin; `subscribe`;
sweep the inbox.

| Need | Op | Notes |
|---|---|---|
| Friday sends | `send {from, to, text, request_id, reply_to?}` | `from` must be declared |
| Receipt, directory | `receipt {from, request_id}`, `directory {query?}` | local |
| Sweep | `inbox.peek {}` → records for every session with waiting messages | **E2**, on `mail::waiting_keys` and `ipc::records` (`framed` included) |
| Hand to the turn | `inbox.ack {session, id, stage: "delivered"}` | **E2**, `take_one`, at turn start |
| Turn ended | `inbox.ack {session, id, stage: "read"}` | **E2**, `mark_read`, at turn end; `forget` then drops the text |
| Re-declare sessions | `cloud.sessions.set {sessions}` | after a `/network/sessions` invoke |
| Invite line | `team.invite {mode: "person", for}` → `{line, expires_at}` | **E8** |
| Hosted app | `app.send|app.inbox|app.receipt {app, person, …}` | **E5** |
| Member holds | `holds`, `hold.get`, `hold.answer {hold, verdict, scope?, decided_by}` | **E6** |
| Snapshot | `snapshot {path}` | path outside the home |
| Stop | sidecar `stop` → `Instance::shutdown` | the `shutdown` op is refused |

**Events:** `message` (→ sweep), `held`/`hold_closed` (→ `hold.get`, then the content-free
notice, job 7), `superseded` (**E1**, section 9). A `Lagged` gap → sweep.

**Save:** 1 s after any event or state-changing op, `snapshot`, skip if the SHA-256 is unchanged,
and put `state.db` with `IfMatch: <ETag>` (or `IfNoneMatch: "*"` when absent). On 412 the sidecar
stops, emits `network.state_conflict`, and the network stays off for this process.

**Stop (SIGTERM, and the 7 h recycle):** the network stop runs on its own thread beside the
profile saves. The bridge sends `stop` (the sidecar treats its own SIGTERM the same
**(unverified: how AgentCore signals child processes)**), waits ≤ 6 s for the final snapshot,
then puts it. If the 10 s run out, the last debounced save stands: at most 1 s plus one put lost.

### 4.5 Jobs

**(1) Friday sends.** The plugin calls the socket to send, read receipts and list the directory.
It derives `from` from the turn's agent id through `sessions.json`, never from the model. Mac
online → `stored`/`held`/`delivered` within about a second; offline → `queued` in the member's
outbox. Friday posts to a room only when she chooses to. The room tool's run-less `eventId`
(4.2) must use the network message id first, or a post after a restart is silently dropped.

**(2) Inbound → a local turn (at-least-once).**
- A Mac sends to `friday` like any session. The message waits on the sender's Mac until the
  member takes it.
- On a sweep, the bridge starts one turn per record through a run-less ingress, `network_message`,
  with `eventId` = the message id and the `framed` text.
- The turn reuses one Hermes session keyed by the sender's thread (the record's `thread`, else
  `from`), the way room turns are keyed by room.
- At turn start the bridge acks `delivered`; at turn end, `read`. If `turn.reserve_turn()` refuses
  (a recycle hold), it does not ack, and the next start runs the message.
- A crash mid-turn runs the message once more on the next start. Spend stays behind the
  openrouter-gate; loops stay bounded by `MAX_HOPS = 4` and `LOOP_LIMIT = 12` per 600 s (`mail.rs`).

**(3) Wake.** `InvokeAgentRuntime {path: "/network/wake"}` only runs `ensure_started()`. The
engine dials every active member on start, and each sender flushes on `hello`, so a start is the
whole wake.

**(4) Connect.**
- Workspace: the backend mints `networkKey` (4.3), writes `sessions.json`, and stops the session.
  The first start writes the solo genesis: the workspace is the founder.
- A computer: Settings → "Connect your computer". The signed-in web session calls a Clerk route;
  the backend invokes `/network/invite {for: <Clerk user id>, name}`; the bridge calls
  `team.invite` (E8) and the page shows the line once.
- The person pastes it. E8 admits the Mac without an Allow, and the member writes a PEOPLE
  `accept` row for that person, so the person's messages to Friday are never held on the member.
- On each Mac, Friday's first message is held once in the native dialog (`policy::decide`); an
  "always" Allow covers the rest.
- Disconnect: stop, then delete `networkKey`, `<owner>/network/*` and the owner's network rows.

**(5) Session list changes.** When the agents or the hosted-app sessions change, the backend
rewrites `sessions.json` and invokes `/network/sessions`; the bridge calls `cloud.sessions.set`.
A rename makes a new address (Plan C's rule). Agents plus persons × apps must fit in 100.

**(6) Hosted assistants through the member.** Hosted MCP → backend → `/network/app {op, app,
person, …}` → `app.*` (E5), from a declared session for that app and person, labelled
"<App> via <Person>". The recipient's `decide` holds it like any first message from a person.

**(7) Holds.**
- A hold on a Mac is answered on that Mac (the dialog shipped in 0.2.x). Nothing else.
- A hold on the member (a stranger's first message, `decide`) shows on the web and phone as a
  Needs-you item, on the webhook-agent hold rows and renderer (`WEBHOOKSEND`,
  `activity.render_webhook_agent_ask`). The notice carries ids, the label and the target only.
- A tap → Clerk decision route → `InvokeAgentRuntime /network/hold {hold, verdict, scope,
  decided_by}` → `hold.answer` (E6).

### 4.6 The doorbell (owner decision PENDING)

**If allowed:** the member publishes `wake: {url, token}` in its own roster entry (E4). The URL is
`https://api.thelightbulb.company/api/v1/network/wake/<token>`. A peer calls it with the system
`curl` (as `update` does; no HTTP crate) only after a direct dial to the member fails. The backend
checks the token (a bad token is a 4xx and nothing else) and one per-workspace limit (one wake per
10 s, a conditional update), then writes a `WORK#NETWAKE#<ulid>` row (payload `{kind:
"network_wake", ownerId}`, its own FIFO group, dedup id = the ulid). `deserialize_pointer` gains
that branch, and the consumer invokes `/network/wake`. No signature, no `at` window, no
per-sender limit. **If not allowed:** there is no doorbell; a message to a parked workspace waits
on the sender's Mac for the workspace's next natural wake (any turn or invocation), up to 24 h.

### 4.7 Two writers

One claimed microVM per owner and the engine's `daemon.lock` keep one writer. Every `state.db`
put is conditional; on a 412 the loser stops. If a split session ever runs two sidecars, peers
keep one link per key (`SUPERSEDED`) and receivers dedupe by envelope id. A cut-off put leaves
the previous object.

### 4.8 The state object

`state.db` is a `VACUUM INTO` copy, read once per process and written after changes. Only it is
persisted; `addrs.json` holds NAT'd hints and is not kept.

| Table | Holds | Size, small workspace |
|---|---|---|
| `roster` | signed entries | ~0.5 KB each |
| `messages` | every network message (≤ 64 KiB) | ~1 KB each while the text is kept; the row stays after `forget` drops the text |
| `receipts` | one per network message | ~0.1 KB each; never pruned |
| `outbox`, `seen_ids` | queued sends and dedupe, 8 days | small |
| `peers_sessions`, `policy`, `holds`, `approvals` | presence, PEOPLE rows, holds | small |

A developer Mac's `state.db` with 11 roster entries and 17 messages is 147 KB. A restore or put
is tens of ms. Re-deriving the state from peers was rejected: a fresh engine writes a new genesis.

### 4.9 Failure modes

| Case | What happens | What the person sees |
|---|---|---|
| Parked runtime; a Mac sends to Friday | dial fails; doorbell (if allowed); cold start; turn | `queued`, then `delivered` in about a minute |
| No doorbell, or it is lost | the message waits for the next wake | longer `queued`; `expired` after 24 h |
| Friday sends; Mac offline | waits in the member's outbox | `queued`, then delivered when both are up |
| Hard kill without SIGTERM | changes since the last save are lost; an un-acked message runs again | at worst one message lost or run twice |
| 412 on put | that sidecar stops | nothing; a metric |
| n0 relays down | members with no direct path queue | `queued` |
| Ping storm or bad token | ≤ 1 wake per workspace per 10 s; 4xx | nothing |
| Undeclared or renamed session | refused `policy` | the sender sees `refused` |
| Stale `state.db` after a reprovision | `KEY_MISMATCH`, unless the seam deleted it (4.3) | "Reconnect workspace" |
| Invite line unused within 24 h | the once link expires | "Connect your computer" again |
| Hosted app; parked runtime | cold start first | the send takes about a minute |

### 4.10 Keepalive

None in phase 1: there is no proof to renew, and the member is a `computer`, so a Mac's presence
check for cloud senders (`mail.rs:725`) does not apply. Proofs and their keepalive are phase 3.

## 5. Latency

| Path | Warm runtime | Parked runtime |
|---|---|---|
| Mac → Friday, to `delivered` | already connected: ≈ **0.1–0.3 s**, plus the turn | ring + row → Pipe → FIFO → Lambda **(unverified ~0.5–1 s)** + cold start **54–57 s measured** + start **~1 s (unverified)** ≈ **~60 s**; with no doorbell, until the next wake |
| Friday → Mac, to `stored` | ≈ **0.2–0.8 s** | n/a |
| Hosted-app send, member hold decision | **(unverified ~0.3–1 s)** | ≈ **~60 s** |
| Directory, receipt | **< 10 ms** | n/a |

The parked case costs the same minute a room message pays today. That is the main trade.

## 6. Cost

| Item | This design | Stateful bridge |
|---|---|---|
| Always-on member compute | **$0** | ≈ $14.40 a month (0.5 vCPU / 1 GiB) |
| Relay | n0 public relays, $0 | ours ≈ $15 a month |
| Secrets Manager | $0 new (a field in the boot secret) | $0.40 a month per workspace |
| S3, DynamoDB, NAT data | pennies | pennies |
| Extra wakes | each one pays the normal 30 min idle timeout | none |

Ceiling: a network-only wake keeps a microVM up to 30 min **(unverified: AgentCore's idle rates
and microVM size)**. Phase 1 measures this first. An early stop is added only if the cost shows.

## 7. Engine changes (v0.3.0)

**E0. Linux.** Build and test on linux/arm64 in CI. Gate the macOS-only parts behind
`cfg(target_os)`: launchd (`daemon --install`/uninstall, `launchctl`, `Library/LaunchAgents`,
`cli.rs`), the `osascript` dialog (`dialog.rs`), process lookup and `peer_pid` (`os.rs`; the
non-macOS stub returns errors), case folding of paths (`presence.rs:117`), the Local Network
doctor row (`cli.rs`), and the `~/.lightbulb/connect` home and `sock` path conventions (`paths.rs`;
the sidecar passes its own). `install.sh` stays Darwin-only.

**E1. `lightbulb member`, the headless sidecar.** Flags `--home`, `--snapshot`, `--socket`,
`--name`, `--relay`, `--sessions <file>`; token on stdin. `embed::start` plus a socket; each line
goes to `Instance::call`; `subscribe` streams events; `stop` and SIGTERM run `Instance::shutdown`.
`EmbedOpts.sessions` is declared before `Net::start` (today an early message is refused, `mail.rs:762`).
A `superseded {by_peer}` event when a peer closes our link with `SUPERSEDED`.

**E2. `inbox.peek {}` and `inbox.ack {session, id, stage}`.** `peek` uses `waiting_keys` and
`ipc::records`. `ack` with `delivered` runs `take_one`; with `read`, `mark_read`.

**E4. The doorbell (if allowed).** An optional `wake {url, token}` on the member's own roster
entry. A computer runs the system `curl` on it after a failed direct dial to that member, with
backoff from 30 s to 15 min while messages are due. Logs never carry the token.

**E5. `app.send|app.inbox|app.receipt {app, person, …}` on the member**, from the declared
session for (app, person), rendered "<App> via <Person>" (`label.rs`; not on the wire). The relay
to a person's computer (`app_send.rs`) is retired.

**E6. Hold ops on an instance.** `holds`, `hold.get` and `hold.answer {…, decided_by}` are
admitted for `Caller::Instance`. `decided_by` replaces `by: "local"` on that one path; the Mac's
dialog and CLI paths are unchanged. No new hold logic: `decide` already holds.

**E7. Member relays off on computers:** no `hold_notice` to the member, no `decision` frames or
`app_*` envelopes from it. The member key is readable by the agents (section 9).

**E8. Invites on the member.** `team.invite {mode: "person", for}` is admitted for
`Caller::Instance` (today `NOT_ON_AN_INSTANCE`). It mints a one-time link that admits without an
Allow. Today once links still wait (`admission.rs:311-314`) and only open links skip it, so this
is a real change. On that join the member writes a PEOPLE `accept` row for the person.

No protocol bump: new ticket and roster fields follow the `#[serde(default)]` pattern.

## 8. What survives from Plan C

| Plan C task | Fate |
|---|---|
| 1 Pin the engine | to build: `network-version.json`, a Rust build stage in the Hermes image (copy the `gocli` cross-build pattern, `Dockerfile:26`), smoke `lightbulb version` |
| 2 Installer, rewrites | to build: the panel shows the engine's installer; the engine binary owns `lightbulb` |
| 3 Issuer | phase 3 (verified names) |
| 4 Relay | not used; n0's public relays |
| 5, 6 Bridge skeleton, VPC door | gone |
| 7 Connect | to build: the invite route, the network rows (in the messaging table's `OWNER#` partition, which both wipes already remove), `/network/invite` |
| 8 Send, inbound | sends on the local socket; inbound as local turns; no room routing |
| 9, 13 Hosted MCP tools | to build; they send through the member (E5) |
| 10 Webhook agents | deferred (their secret lives at the backend, which posts) |
| 11, 20 Holds and Allow UI | member holds only, on the webhook-agent hold rows |
| 12 Bridge CDK | gone |
| 14 Friday's tool | to build, on the local socket; one tool beside `lightbulb_local_sessions`, named so the model cannot confuse them |
| 15–17 App | gone |
| 18–19 Streams | phase 2 |
| 21–24 | re-scoped acceptance |

## 9. Security

**The member key is the founder's key, and the agents can read it** (the boot secret, 4.2; and
`identity.key` on disk). A prompt-injected agent that takes it can run an impostor engine outside
AWS: send as any declared session, read their messages, and act as founder (mint links, admit,
remove, make admins), because at its own socket it is a person. E7 stops it relaying decisions or
acting as a person through a Mac. A key the agents cannot read needs a second uid with its own
credentials, and AgentCore gives a runtime one execution role.

**Impostor detection and revocation.** The impostor and the runtime supersede each other on every
redial. More than 3 `superseded` events in 10 min with no split session raises
`network.member_key_contested` (page tier). The founder cannot `team.leave`, so revocation is
"Reconnect workspace": a new key, a new team, and every Mac pastes a new line.

**Inside one workspace, agents are one principal.** The socket needs the control token, held only
in bridge memory. Roster entries are signed, so writing `state.db` in S3 cannot forge membership
without the key.

**The doorbell (if allowed) is the one public surface.** It needs the token, it has one
per-workspace rate limit, and it can only cause a start. A bad token is a 4xx and nothing else.

**The invite line** is a credential for that person, shown once over their signed-in session. It
admits one computer within 24 h without an Allow (E8).

**Member hold decisions** carry `decided_by` = the Clerk user of the tap. The backend checks the
user against the hold row; the engine records it (E6).

## 10. Decided

- Relay: n0's public relays; ours only if customers need it.
- Inbound to Friday: a local turn, in a session keyed by the sender's thread; not posted to rooms.
- Receipts: `delivered` when the message goes to Friday's turn, `read` when the turn ends.
- The key: minted once by the backend, kept by the boot-secret writer, never uploaded.
- State at rest in the owner's S3 prefix: accepted. Streams: not in phase 1.
- Cut from phase 1: `pull`, `queued_for`, the state route, `network.busy()`, the early stop,
  webhook agents, the keepalive, and persisting anything but `state.db`.

Phases: (1) everything in this document; (2) session streams to the web and phone, through the
member, to AppSync; (3) phone approval of holds on a Mac, verified names (the issuer, the key
ceremony), and key rotation.

## 11. Task list (phase 1)

1. Engine E0: the Linux (arm64) port, Linux in CI, and a build stage in the Hermes image.
2. Engine E1: the `member` sidecar, sessions before start, `superseded`.
3. Engine E2: `inbox.peek`, `inbox.ack` with a stage.
4. Engine E5, E6, E7, E8: hosted apps on the member, hold ops on an instance with `decided_by`, member relays off, Settings invites with the PEOPLE row.
5. Engine E4 and the doorbell route, its `WORK#NETWAKE#` row and pointer branch: only if the owner allows it.
6. Pin v0.3.0 (`network-version.json`) and the image path filter.
7. Backend: `networkKey` in `write_boot_secret`; delete `<owner>/network/` in the reprovision and wipe seams.
8. Bridge `network.py`: start trigger and lock, restore, sweeps, conditional saves, 412, the SIGTERM stop thread.
9. Bridge handlers: `/network/wake`, `/network/invite`, `/network/sessions`, `/network/app`, `/network/hold`.
10. Bridge: the `network_message` ingress, the thread-keyed session, the two acks.
11. Plugin: Friday's network tool on the socket; the run-less room `eventId` fix.
12. Backend: the connect flow, `sessions.json`, the invite route.
13. Web: the Settings "Connect your computer" panel (installer, line shown once, copy).
14. Backend and web: member holds as Needs-you items, on the webhook-agent hold rows and renderer.
15. Backend: hosted MCP tools send through the member.
16. `hermes-version.json` pin flip (dev, then prod) and `roll_hermes_runtimes.py`.
17. Acceptance: a pasted line, a parked-runtime message, an offline Mac, a hosted app with the Mac off, a member hold decided on the phone, the 7 h recycle, the key-contested alarm. Measure the wake cost.

## Rejected alternatives

- **The stateful Fargate bridge (Plan C).** Instant inbound, but always on, a second
  single-writer state, and its lease and snapshots raced in review.
- **A Rust Lambda per job.** It rebuilds the runtime's lock, state transfer and wake, and one role
  could read every workspace's key.
- **Hosted apps through the person's paired Mac (revision 2).** New app commands, a pairing switch,
  and it failed whenever the Mac was off.
- **A Mac↔backend channel for phone approval of Mac holds (revision 2).** It put a backend channel
  into every Mac. Phone approval of Mac holds is phase 3.

## Index: builder check (rev 3) and where each item is resolved

- T1 key lifecycle: 4.3 (minted once, kept by the writer, a stop after connect, the reprovision delete).
- T2 founding and holds: 4.2, job 4, E6 (`decide` already holds; PEOPLE row on an E8 join).
- T3 call home: 4.6 (system `curl`, if allowed; owner decision pending); no `--issuer`.
- T4 receipts: E2 two stages; `forget` then drops the text.
- T5 Linux: E0, task 1.
- T6 installer clash: no desktop app; the panel shows the engine's installer (8, row 2).
- T7 session per message: job 2, keyed by the sender's thread.
- T8 webhook agents: deferred.
- T9 pull, `queued_for`, state route, busy, early stop: cut; wake cost measured first (6).
- T10 signature: dropped (4.6). T14 roster-entry token: kept, per the ruling (E4).
- T11 two sessions tools: 8, row 14. T12 run-less room event id: job 1, task 11.
- T13 which workspaces: owner to decide.
- M1 rows in the `OWNER#` partition; M2 reuse; M3 `waiting_keys`; M4 the 100 cap; M5 `hold.get`;
  M6 no ack on a recycle hold; M7 only `state.db`; M8 a new work kind; M9 the wipe seam; M10 one
  limit, the ulid as dedup id.
- W1–W7: fixed in 4.2, 4.3, 4.8, 7 and 8.

## Unverified in this draft

- UDP and direct QUIC from an AgentCore VPC-mode microVM through the NAT gateway (the relay
  fallback is TCP 443).
- AgentCore's idle-session billing, its rates, and the microVM's size.
- How AgentCore delivers SIGTERM to child processes. The design works either way.
- Engine start and stop timings in the microVM; the stream → Pipe → FIFO → Lambda hop; the warm
  hosted-app and hold-decision paths.
- The size of the Linux port (E0).

## Owner to decide

1. The doorbell: allow one call home (system `curl`, token URL in the member's roster entry), or no doorbell, and parked workspaces wait for their next wake.
2. Which workspaces get a member: every workspace, paid only, or trials too.
3. Accept that the agents can read the founder key (impostor risk with detection; revocation means a new team).
