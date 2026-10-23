# Lightbulb Connect: the workspace member inside the AgentCore runtime (design)

Status: draft for the owner, revision 2. It replaces the stateful "bridge" in Plan C (Tasks 5
and 6, the bridge half of 7, and the bridge parts of 8–14). Nothing here is built. Revision 2
resolves every item in `.superpowers/sdd/2026-10-22-lightbulb-network-plan-c-bridge-app-cutover/design-builder-check.md`
(see the index at the end).

Words: a **network message** is the engine's store-and-forward message between members (the
engine's code calls it mail: `mail.rs`). This document never means email.

Sources read:
- the engine at `lightbulb-network.nosync` `main` (v0.2.0): `src/embed.rs`, `src/daemon/ipc.rs`,
  `src/daemon/net_ops.rs`, `src/net/mod.rs`, `src/net/frames.rs`, `src/mail.rs`, `src/outbox.rs`,
  `src/decision.rs`, `src/app_send.rs`, `src/presence.rs`, `src/proof.rs`, `src/roster.rs`,
  `src/store.rs`;
- the Hermes image: `apps/infra/hermes/Dockerfile` and `bridge/{__init__,server,sync,recycle,turn}.py`,
  and `plugins/lightbulb/__init__.py`;
- the backend: `routers/gateway_relay.py`, `core/services/{node_broker,machine_auth,desktop_pair,openclaw_work,mac_browser,agentcore_runtime,runtime_transport}.py`,
  `core/repositories/messaging_repo.py`, and the built issuer `network_issuer.py` / `routers/network.py`
  (Plan C Task 3, #4299);
- infra: `container-stack.ts`, `network-stack.ts`, `api-stack.ts`, and `lambda/messaging-projector/index.py`;
- Plan C, the workspace-member ruling, the built `apps/bridge/` crate, and the Task 5 and Task 7
  reviews.

Claims I could not check in code are marked **(unverified)**. Decisions only the owner can make
are marked **owner to confirm**.

---

## 1. Intent and why

Plan C runs the workspace member as an always-on Fargate task that holds every workspace's key,
with S3 snapshots and a DynamoDB lease. The reviews found that the lease and the snapshots race:
two bridges could run one key (Task 5 HIGH-1), and a stale snapshot could overwrite a newer one
(HIGH-2).

The owner's rule: stateless, cheaper, on AWS. Every workspace already has exactly one stateful,
single-writer, S3-backed, wake-on-invoke compute unit, which is its AgentCore runtime. The member
moves in there.

## 2. What must exist in the cloud at all

One thing: **the workspace's agents live in the cloud, so the network messages sent to them must
be received in the cloud.** Something has to hold the workspace member's key and keep its outbox
and seen-ids. Peers must be able to wake it.

Two things Plan C put in the member do not need it:
- **Hosted apps acting as a person.** They act through the person's own Mac. The backend already
  has an authenticated channel to that Mac (the desktop node relay on `bv-`).
- **Phone and web decisions.** The hold is on the person's Mac. The backend hands the tap to that
  Mac over the same channel.

The relay is still a server (section 10). It holds no state.

## 3. What goes through the backend, and what never does

This is the owner's rule.

| Through the backend | Never through the backend |
|---|---|
| Name proofs (the issuer, Task 3) | Network message bodies between members |
| Wake signals, which carry no content (the doorbell, 4.6) | Hold text (hold events carry ids, the sender label and the computer only) |
| Runtime invocations (`InvokeAgentRuntime` envelopes with no message content) | The roster |
| A hosted app's requests to the person's own Mac, carrying only the text that hosted app itself sent | Member keys. The workspace member key lives only in the owner's boot secret (4.3) |
| Phone decisions: hold ids, verdict, scope and `decided_by` | Streams |

What this means for the design:
- **Inbound network messages to Friday run as local turns inside the runtime** (4.5 job 2). They are not
  posted into a room. Plan C Task 8 routed them into room threads through the backend, and that
  routing is dropped. Consequence: people in a room do not see a Mac's reply word for word unless
  Friday posts about it in her own words. **Owner to confirm.**
- **The engine's state sits at rest in the owner's S3 prefix** (`<owner>/network/`), next to the
  Hermes profiles. Backend code never reads it, but the backend task role can read that bucket
  (it writes profiles there). **Owner to confirm** that "at rest in the owner's prefix" passes
  the rule.
- **Streams:** this design adds no stream path. Today's shared-session streams use the
  backend's desktop relay. Moving them is a separate decision.

## 4. The design

### 4.1 Components

| Piece | Where | New or existing |
|---|---|---|
| `lightbulb-connect member`, the sidecar | baked into the Hermes image and started by the bridge | binary exists; subcommand is new (E1) |
| Engine state `<owner>/network/{state.db,addrs.json,relays.json,relays.json.minisig}` | `OWNER_STATE_BUCKET` | new objects; existing bucket and `<owner>/*` grant |
| Declared sessions `<owner>/network/sessions.json` | written by the backend | new, backend-owned (like `config.yaml`) |
| Member key `networkKey` | the owner's boot secret | new field, existing secret |
| `bridge/network.py` | the Python bridge | new |
| `lightbulb_sessions` tool | `plugins/lightbulb/`, which calls the sidecar socket in process | reshaped from Plan C Task 14 |
| Doorbell `POST /api/v1/network/wake/<token>` | backend | new |
| `network_wake` `WORK#` row | the messaging table → Pipe → `openclaw-work.fifo` | new kind on the existing path |
| Hook-token route `POST /api/v1/internal/network/{owner}/state` | backend (public API, hook token) | new; carries no content |
| Mac commands `network.app.v1` and `network.hold.answer.v1`; event `network.hold.v1` | desktop app over the node relay | new commands on an existing channel |
| Relay | Fargate (Plan C Task 4) or n0 | unchanged |

There is no Lambda, no lease, no VPC-only listener and no always-on member process.

### 4.2 Verified facts this builds on

Runtime:
- **The warm pool.** "Nothing session-bound happens before the first /invocations: AgentCore's
  warm pool runs this CMD on ~10 pooled instances per runtime version" (`server.py:13-14`).
  `bridge/__init__.py` reads the boot secret at import, on pooled instances too.
- **One session per owner.** The session id comes from the owner id, and a cold start is
  single-flight, so one claimed microVM per workspace receives `/invocations`
  (`runtime_transport.py`; CLAUDE.md).
- **Timings.** `IDLE_TIMEOUT_S = 30*60` and `MAX_LIFETIME_S = 8*60*60` (`agentcore_runtime.py:117-119`).
  The bridge recycles at 7 h (`recycle.py`). The caller's budget is 90 s
  (`runtime_transport.py:41`). A cold workspace measured 54–57 s (CLAUDE.md).
- **Busy and stop.** `/ping` answers `HealthyBusy` while `turn._busy` or `followups.busy()`
  (`server.py`, `_ping_status`). On SIGTERM AWS gives ≤ 15 s, and the bridge saves every
  restored profile within `SIGTERM_SAVE_S = 10.0` (`server.py:781-803`).
- **The agents can read the boot secret.** It is read with the execution role. The agent's shell
  can read that role's credentials over the metadata service (comment in `bridge/__init__.py`).
- **Conditional puts.** S3 conditional puts (`IfMatch`) are already used (`sync._upload_object`).
- **Egress.** The runtime security group allows all outbound (`container-stack.ts`); the VPC has
  one NAT gateway (`network-stack.ts`).

Work path:
- The Pipe forwards only messaging-table inserts whose `sk` begins `WORK#` (`api-stack.ts:1028`).
  It takes the FIFO group and dedup ids from the row's `messageGroupId` and
  `messageDeduplicationId` (`api-stack.ts:1053-1055`).
- `openclaw_work.deserialize_pointer` reads only the stream record's `NewImage.payload.M`, and
  it refuses any unknown `kind` (`openclaw_work.py:104-155`).
- The projector ignores every `sk` that is not `MSG#` or `LIVE#` (`messaging-projector/index.py:1124`).

Engine:
- `embed::start` runs a `kind: cloud` instance and opens no socket. `Instance::shutdown` closes
  the endpoint, waits up to 5 s for its tasks, then writes the final snapshot to
  `EmbedOpts.snapshot`, which must be outside the home (`embed.rs`).
- `Instance::call` runs `ipc::op` as `Caller::Instance`. `NOT_ON_AN_INSTANCE` refuses
  `team.invite`, `reload`, `ui.claim` and `shutdown` (`ipc.rs:166`).
- The socket path intercepts `inbox` before `op()` and runs the computer form (`ipc.rs:141`).
  The cloud form, `embed::inbox {session}`, is reached only as `Caller::Instance` (`ipc.rs:208`).
  It takes the messages and marks each one read (`embed::take_read`).
- `mail::waiting` reads the waiting messages and leaves them in place. `mail::take_one` takes one
  by id and moves its receipt to `delivered` (`mail.rs:866-929`). `unread {key}` only counts
  (`ipc.rs:325`). `mark_read {key}` marks every delivered message of the session (`mail.rs:942`).
- The `message {to_session, id}` event is sent only for a new network message (not a resend), on
  a 256-slot broadcast (`ipc.rs:677-684`; `embed.rs:134`). It can be missed.
- On start the engine dials every active member at once, then every 30 s. Macs dial the cloud
  member the same way (`net/mod.rs`, `keep_up`).
- Queued network messages wait in the **sender's** outbox. They are retried only to online members,
  for `TTL_S = 86_400` (`outbox.rs`). On a `hello` the sender flushes all of them (`flush(all)`).
  There is no end-of-queue marker.
- A cloud instance refuses (`policy`) a network message for a session it has not declared
  (`mail.rs:762`). A Mac refuses one from a cloud `from` that is missing from the member's stored
  presence (`mail.rs:725`). A Mac drops a presence after a day unseen (`presence.rs`, `stored`).
- One key keeps one live link per peer: a newer connection closes the older with `SUPERSEDED`
  (`net/mod.rs:465-482`).
- `identity.proof` is refused unless the caller is a person (`net_ops.rs:151`), so an embedded
  caller cannot use it. The engine renews its own proof at 20 h while it runs (`proof.rs`, `tick`).
- A computer's `person_id` is its proof's `sub` (`roster.rs:444`). The issuer mints a computer
  proof with `sub` = the approver's Clerk user id (`routers/network.py:67`,
  `network_issuer.computer_proof(ctx.user_id, …)`).

### 4.3 The member key

- **Where it lives:** only in the owner's boot secret, as `networkKey` (a base64 Ed25519 seed).
  The backend never holds it.
- **Who makes it:** the runtime, at connect (4.5 job 4). The sidecar generates it, and the bridge
  writes it into the boot secret with a read-modify-write that keeps `hookToken` and
  `gatewayToken`. This needs `secretsmanager:PutSecretValue` on the owner's own boot secret ARN
  for the per-owner role **(unverified: the role does not have it today, as far as I can tell)**.
  **Owner to confirm** this grant. The fallback is that the backend generates the key and writes
  it into the boot secret, but then the key passes through the backend once, which breaks the
  section 3 rule.
- **Reading it:** `bridge/__init__.py` already loads the boot secret at import. `networkKey` is
  kept in module state (never `os.environ`), like `GATEWAY_TOKEN`. On disk it exists only as the
  sidecar's `identity.key` (0600) in the engine home, and it is excluded from every S3 put.
- **Reprovision:** a full `DELETE`/`POST /debug/provision` mints a new boot secret, which loses
  `networkKey`. The workspace then shows "Reconnect workspace". This is accepted.

### 4.4 Bridge ↔ engine contract (op by op)

The sidecar (E1) is `embed::start` plus a Unix socket. Every line on that socket goes to
`Instance::call`, so every op runs as `Caller::Instance`, and `inbox` takes the cloud form
(`ipc.rs:208`). Each request carries `token`, the per-boot control token. The sidecar strips it
before `call` and refuses a line without it. The token lives only in the bridge's memory, so an
agent's shell cannot use the socket even when it can reach the file.

**Start trigger.** The engine starts only from inside `handle_plugin`, that is, on a real,
authorized `/invocations` (never at import or at `main()`), and only when both of these hold:
`networkKey` is in the boot secret, and `<owner>/network/sessions.json` exists.
- A pooled microVM never gets an `/invocations` until AgentCore claims it for the owner's session
  (`server.py:13-14`). Only one microVM per owner is claimed (the session-id rule). So two pooled
  microVMs can never both start an engine with one key.
- `network.ensure_started()` runs under one process-wide lock. For a `/network/*` path it is
  synchronous (restore and start take about 1 s **(unverified)**, well inside the 90 s budget).
  For any other path it starts in a background thread and does not delay the turn.
- If the engine fails to start (for example `lock_dir` finds a leftover lock), that is fatal for
  this process's network: it logs, emits `network.start_failed`, and retries on the next
  invocation after 60 s. It never starts a second sidecar.

**Start sequence:**

| Step | Bridge does | Engine op / fact (line) | Gap |
|---|---|---|---|
| 1 | `GetObject` `state.db`, `addrs.json`, `relays.json(.minisig)`, `sessions.json`. Keep `state.db`'s ETag, or "absent" | — | — |
| 2 | Write `identity.key` 0600 from `networkKey` into `/opt/data/network/` | engine reads `identity.key` from home (`identity.rs`, `load_or_create`) | — |
| 3 | Spawn `lightbulb-connect member --home /opt/data/network --snapshot /opt/data/network-out/state.db --socket /opt/data/network.sock --name <ws name> --relay <url> --issuer <url> --sessions <sessions.json>`, with the token on stdin | `EmbedOpts {kind: Cloud, relay, issuer, snapshot, name}` (`embed.rs`) | **engine change E1:** the subcommand; `EmbedOpts.sessions`, declared before `Net::start`; presence before envelopes |
| 4 | `subscribe` | `"subscribe" if instance` (`ipc.rs:207`) | **E1:** the sidecar streams `Instance::events()` to a subscribed line |
| 5 | Inbox sweep: `inbox.peek {session}` for every declared session | — | **engine change E2:** `inbox.peek` |

**Running:**

| Need | Op | Line | Notes |
|---|---|---|---|
| Friday sends | `send {from: "<ws short>/<slug>", to, text, request_id, reply_to?}` | `ipc.rs:280` | `from` must be declared (`ipc.rs`, `not_declared`) |
| Receipt | `receipt {from, request_id}` | `ipc.rs:287` | |
| Directory | `directory {query?}` | `ipc.rs:303` | |
| Read inbound without taking | `inbox.peek {session}` → `{messages: [INBOX_RECORD]}` | — | **E2.** Built on `mail::waiting`; records from `ipc::records` (`ipc.rs:784`), so each one already carries `framed` (`INBOX_RECORD`, `ipc.rs:170`) |
| Acknowledge inbound | `inbox.ack {session, ids: [id]}` → `{acked: [id]}` | — | **E2.** `mail::take_one` for each id: receipt `delivered`. Never `read` (Q2) |
| Re-declare sessions | `cloud.sessions.set {sessions}` | `ipc.rs:209` | after a `/network/sessions` invoke (4.5 job 5) |
| Pull | `pull {members: [key], quiet_ms, deadline_ms}` → `{connected, received, quiet}` | — | **engine change E3** |
| What is queued for whom | `status` → `queued_for: [member key]` | `status` exists (`ipc.rs:213`) | **E3:** add `queued_for` |
| Snapshot | `snapshot {path}` | `net_ops.rs:200-203` (admitted for `Caller::Instance`) | path outside the home |
| Join | `team.join {ticket, name, kind: "cloud", proof}` | `ipc.rs:312` (`for_a_person` is true for an instance) | waits up to `JOIN_WAIT = 600 s` |
| Leave | `team.leave {}` | `ipc.rs:333` | |
| Stop | sidecar-level `stop` (not an engine op) → `Instance::shutdown` | `embed.rs` | `shutdown` itself is refused (`ipc.rs:166`); see the stop sequence |

**Events the bridge acts on:** `message` (→ peek that session), `receipt_changed`,
`member_removed` (its own key → post state `removed`), and `superseded` (**E1:** a new event,
section 9, risk 10). On a `Lagged` gap on the event line, the bridge runs an inbox sweep.

**Save (event-driven):** after any event, and after any `send`, `inbox.ack`, `pull` or `team.*`,
the bridge waits 1 s (debounce). Then it calls `snapshot {path: /opt/data/network-out/state.db}`,
skips the put if the bytes are unchanged (SHA-256), and puts `state.db` with `IfMatch: <ETag>`,
or with `IfNoneMatch: "*"` when it was absent at restore. `addrs.json` and the relay cache are
put unconditionally (they are hints). After a put, if `queued_for` or `proof_exp` changed since
the last post, it posts `/internal/network/{owner}/state {queued_for, proof_exp, state}`
(hook token, no content).

**On 412** (another writer): the bridge sends the sidecar `stop`, does not put again, emits
`network.state_conflict`, and leaves the network alone for this process.

**Stop sequence (SIGTERM, and the 7 h recycle, which also SIGTERMs):**
1. `_save_on_sigterm` starts the network stop on its own thread, in parallel with the profile
   saves, so the profiles keep their full 10 s.
2. The bridge sends the sidecar `stop`. The sidecar also treats its own SIGTERM as `stop`, so
   the order is the same whether or not AgentCore signals the whole process group
   **(unverified: how AgentCore delivers SIGTERM to child processes)**.
3. The sidecar runs `Instance::shutdown`: it closes the endpoint (no more acks or flushes),
   waits ≤ 5 s for tasks, writes the final snapshot to `--snapshot`, and exits.
4. The bridge waits for the exit (≤ 6 s), then makes the conditional put of `state.db` (≤ ~1 s for a
   few MB), then posts state if time remains.
5. If the 10 s run out first, the last debounced save stands. The loss is bounded by the 1 s
   debounce plus one put.

There is no stop at the end of a turn. Saves are event-driven, so a turn adds nothing.

**Busy:** `network.busy()` is a third term in `_ping_status` (`server.py:772-775`): true while a
join waits or a pull runs. It is NOT `turn._busy`, so a recycle's `hold_turns` and idle wait are
unchanged. A recycle at 7 h can therefore cut a join short. The join then answers `unreachable`,
and the connect flow shows "try again". This is accepted, because a join runs on a fresh session.

### 4.5 Jobs

**(1) Friday sends** (`lightbulb_sessions` `send`; webhook agents, Task 10).
- The tool runs in the bridge's process. It derives `from` from the turn's agent id through
  `sessions.json`, not from the model's arguments. It calls `send`.
- Mac online → `stored`/`held`/`delivered` within about a second. Mac offline → `queued` in the
  member's outbox, saved with the state, and shown in `queued_for`.
- `directory` and `receipt` are local. No backend route is used.

**(2) Inbound network messages → a local turn (at-least-once).**
- Trigger: a `message` event, a start sweep, a post-pull sweep, or a `Lagged` sweep. The bridge
  calls `inbox.peek {session}` and starts one turn per record through a new run-less ingress,
  `network_message` (like agent-mail ingress: no `run` block), with
  `eventId = <network message id>`, `targetAgentId` from `sessions.json`, and the record's
  `framed` text.
- `turn.register_run("event:<id>")` dedupes within the process.
- When the turn ends (any outcome), the bridge calls `inbox.ack {session, ids: [id]}`, and the
  receipt moves to `delivered`.
- If the microVM dies mid-turn, the record is still waiting, and the next start runs it again.
  At most one repeat per crash is accepted.
- The turn's final answer is sent back over the network as a reply (`send`, `reply_to` = the
  inbound id, from the same agent session) unless it is empty or `[SILENT]`. This mirrors a room
  turn, whose answer posts to the room. Nothing is posted to the backend except usage counts on
  the existing `/internal/messaging/usage/{ownerId}` route (no content).
- Inference spend stays behind the openrouter-gate as for any turn. Loops stay bounded by the
  engine's hop and rate limits (`mail.rs`: `MAX_HOPS = 4`, `LOOP_LIMIT = 12` per `LOOP_WINDOW_S = 600`).

**(3) Pull after a doorbell.** `InvokeAgentRuntime {path: "/network/pull", body: {from, reason}}`.
- The bridge runs `ensure_started()` synchronously, then a sweep, and answers 202.
- On a thread: `pull {members: [from], quiet_ms: 1500, deadline_ms: 20000}`, then a sweep, then a
  save.
- A second wake during a start waits on the same lock and finds the engine running. A second
  pull while one runs simply runs: `pull` is idempotent.

**(4) Connect and disconnect.**
- Connect, step 1: the backend invokes `/network/connect {step: "key"}`. The bridge makes a fresh
  home and starts the sidecar with `--new-key` (**E1:** generate `identity.key` and report the
  public key; `Identity::load_or_create` already generates one on first run). The bridge writes
  `networkKey` into the boot secret and answers `{member_key}`.
- Connect, step 2: the backend mints the `kind=workspace` proof for that key (Task 3, with the
  `wake` claim) and writes `sessions.json`. It invokes `/network/connect {step: "join", ticket, proof}`.
- The bridge calls `team.join` and answers 202. `network.busy()` holds the session for up to
  600 s. Then `cloud.sessions.set`, a save, and a state post `{state: joined|refused, team_id}`.
- Disconnect: `/network/disconnect` → `team.leave` → `stop`. The backend then removes
  `networkKey` from the boot secret, deletes `<owner>/network/*`, deletes the `NETWAKE#` row and
  the owner's network rows.

**(5) Session list changes.** When the roster's agents or the webhook agents change, the backend
rewrites `sessions.json` and invokes `/network/sessions`. The bridge re-reads it and calls
`cloud.sessions.set`. A parked runtime picks up the change at its next start.

Names follow Plan C's rule: `<slug of the display name>`, and a rename makes a new address. A
network message to the old name is refused `policy`. **Owner to confirm** (Q3).

**(6) Hosted app as a person (NOT through the member).**
- `send_network`, `check_inbox` and `message_receipt` (Task 9) go: backend → the person's paired
  desktop app → the app's local engine. They use a new Mac command
  `network.app.v1 {op: send|inbox|receipt, app, to?, text?, request_id?, reply_to?}`, sent with
  `node.invoke.request` (`node_broker.invoke`) and forwarded to the backend task that holds the
  Mac's socket, the way `mac_browser._forward_mac` does.
- The app calls engine `app.local` (**engine change E5**), the computer half of `app_send.rs`,
  which sends as `<mac short>/<app>-app`.
- No online paired Mac → `none of your computers is online right now`.
- A Mac whose app is too old, or whose pairing predates the grant →
  `Update the Lightbulb app on your Mac to use this from <App>.`
- There is no cold start on this path.

**(7) Phone and web decisions (NOT through the member).**
- The desktop app turns the engine's `held` and `hold_closed` events into a node event
  `network.hold.v1 {hold_id, kind, from, label, target_session, computer, created_at}` (no text).
  `network_holds.on_notice` (Task 11) writes `NETHOLD` and the push.
- A tap → `POST /api/v1/network/holds/{id}/decision` (Clerk) → `network.hold.answer.v1 {hold_id,
  verdict, scope, decided_by: <Clerk user id>}` → engine `hold.answer` with `decided_by`
  (**engine change E6**).
- The engine applies it only when `decided_by` equals its own `person_id`, which is the proof's
  `sub` and therefore the same Clerk user id (4.2), so no mapping table is needed. A Mac that is
  not signed in has `person_id` = its own key, so the check refuses.
- Mac offline → `Your Mac is offline. Answer there when it is back.` Nothing is queued.

### 4.6 The doorbell

Semantics: **"connect to me now"**, not "you have mail". The cloud member connects to the member
that rang, takes what it flushes, and flushes its own outbox to it.

**Who rings (engine change E4, on computers):**
- `queued`: the computer has outbox rows due for a cloud member that is not online and whose
  proof carries `wake`. It rings at once, then backs off from 30 s, doubling up to 15 min, while
  rows are due.
- `online`: its home relay connection came up, and a cloud member with `wake` is not online. At
  most once per cloud member per 10 min.

**Where it rings:** `POST {issuer}/wake/<token>`, that is,
`https://thelightbulb.company/connect/wake/<token>`, which the issuer rewrite (Task 2) maps to
`/api/v1/network/wake/<token>`.
- `wake` is a claim in the workspace proof: 256 random bits, base64url, signed by the issuer.
- The engine builds the URL from its own configured issuer, so no member can aim pings at another
  host.

**The ping:** JSON, 3 s timeout, no retry beyond the backoff.
```json
{"v": 1, "from": "<member key>", "to": "<cloud member key>",
 "reason": "queued" | "online", "at": 1793000000, "sig": "<Ed25519 over the canonical JSON without sig>"}
```

**The backend** always answers `202 {}`.
1. `GetItem NETWAKE#<sha256(token)>` → owner. If there is none, stop.
2. Drop the ping unless `to` equals the workspace's member key, `|now − at| ≤ 300`, and the
   signature verifies against `from` (the issuer already verifies this signature form for
   `/proof/refresh`). There is **no** membership filter. It could not be fresh (a member admitted
   while the runtime is parked would never get through), the token already limits who knows the
   URL, and the rate limits bound the cost.
3. `reason = online` and `from` is not in the last posted `queued_for` → stop. This filter
   fails open: the post is made after every save, and a missing post means "wake".
4. Rate limits: one accepted ping per (owner, from) per 5 s; one wake per owner per 10 s
   (conditional update on `NETWORKSPACE.last_wake_at`).
5. Write the `WORK#` row (it rides the Pipe; there is no direct `SendMessage`, so no second event
   bus):

| Attribute | Value |
|---|---|
| `pk` | `OWNER#<owner>` (`owner_pk`) |
| `sk` | `WORK#NETWAKE#<ulid>` |
| `payload` | `{kind: "network_wake", ownerId, from, reason}` |
| `messageGroupId` | `sha256("network|<owner>")`, its own group, beside the rooms and `runtime|<owner>` |
| `messageDeduplicationId` | `sha256("netwake|<owner>|<from>|<at // 10>")` |
| `expiresAt` | as `_expires_at()` |
| condition | `attribute_not_exists(sk)` |

6. `deserialize_pointer` gains a `network_wake` branch: the exact key set
   `{kind, ownerId, from, reason}`, `from` 52 lowercase base32 characters, and `reason` one of the two
   values. The consumer invokes `/network/pull`. The projector ignores the row (`index.py:1124`).

**Token life:** every connect mints a new token and a new `NETWAKE#` row and deletes the old row.
A disconnect deletes the row. A ping with a dead token stops at step 1, even though old proofs
still carry it until they expire. The issuer can revoke a token at any time by deleting the row.

**Early stop after a network-only wake:** the bridge asks for its usual recycle stop
(`runtime/{OWNER}/recycle`) only if the wake's pull received nothing, no turn ran, and no other
invocation arrived within 5 min. If anything was received, it does not stop, so the local turn
never gets a 503.

**Two doorbell routes, for the owner (section 10):** the HTTPS token above works for every
member, CLI-only installs and other workspaces' Macs replying to Friday included. A node event
over the paired channel needs no public route, but it only reaches this workspace's paired desktop
apps.

### 4.7 Two writers

- One claimed microVM per owner, and the engine's own `daemon.lock`, keep a single writer.
- Every `state.db` put is conditional (4.4). On a 412 the loser stops.
- If a split session ever runs two sidecars, peers keep one link per key (`SUPERSEDED`,
  `net/mod.rs:465-482`), and receivers dedupe by envelope id (`seen_ids`). Detection already
  exists in `docs/runbooks/runtime-split-brain-detection.md`.
- A save cut off mid-put leaves the previous object: one S3 put replaces one object atomically.

### 4.8 The state object

`state.db` is a `VACUUM INTO` copy. It is read once per microVM process and written after changes.

| Table | Holds | Size, small workspace |
|---|---|---|
| `roster` | signed entries; one `proof` entry per member per day | ~0.5 KB per entry without a proof, ~1.5 KB with one: ~1.3 MB a year at 3 members, ~4.4 MB at 10 |
| `messages` | every network message sent or received (≤ 64 KiB) | ~1 KB each; **never pruned** (no code deletes these rows outside a test) |
| `receipts` | one per network message | ~0.1 KB each; never pruned |
| `outbox`, `seen_ids` | queued sends and dedupe, 8 days | small |
| `peers_sessions` | the last presence of each member | 1–30 KB per member |

Measured: a developer Mac's `state.db` with 11 roster entries and 17 messages is 147 KB. Estimate:
0.2–1 MB in the first months, a few MB after a year. A restore or a put is tens of ms, once per
process. Growth (roster compaction, pruning) is Plan D.

Re-deriving the state from peers was rejected. A fresh engine writes a solo genesis
(`embed::boot`), and the outbox, `seen_ids` and own-send receipts exist nowhere else.

### 4.9 Failure modes

| Case | What happens | What the person sees |
|---|---|---|
| Parked runtime; a Mac sends to Friday | the Mac queues and rings `queued`; cold start; pull; local turn | `queued`, then `delivered` in about a minute; Friday's reply after her turn |
| Doorbell lost | the Mac re-rings with backoff up to 15 min; any later wake also connects | longer `queued`; after 24 h `expired` (`unreachable`) |
| Friday sends; Mac offline | waits in the member's outbox; `queued_for` posted | Friday's tool reports `queued`; when the Mac comes online it rings `online` and the runtime wakes and flushes |
| Park or recycle with network messages queued | SIGTERM stop sequence saves them | nothing, unless no wake comes within 24 h |
| Hard kill without SIGTERM | changes since the last save are lost (≈ 1 s plus one put); an un-acked inbound one comes back on the next start | at worst one network message lost or run twice |
| 412 on put | that sidecar stops | nothing; a metric |
| Relay down | no member reaches another; everything queues | Friday: `queued`; stale directory; hosted-app sends queue on the Mac |
| Duplicate pings | dedupe id, rate limits, stale `at`; `pull` is idempotent | nothing |
| Ping storm | ≤ 1 wake per owner per 10 s | nothing; cost in section 6 |
| Network message to an undeclared or renamed session | refused `policy` (final) | the sender sees `refused` |
| Presence dropped on a Mac (a day unseen) | E1 sends presence before any envelope on a new link, so the next send is accepted | nothing |
| Proof lapsed after a long park | the engine renews on start (`proof::tick` on the first flush, inside the issuer's 7-day grace); the keepalive (4.10) keeps it inside the grace | nothing; past the grace, "Reconnect workspace" |
| Recycle during a join | the join fails | "Try again" on the connect sheet |
| Hosted app; no paired Mac online | answered at once | `none of your computers is online right now` |

### 4.10 Keepalive

For a connected workspace whose last state post is older than 20 h, the backend's scheduled
worker writes a `network_wake` row with `reason: "keepalive"`, `from` = the member's own key.
The bridge treats it as a start plus a 10 s pull of every member.

This renews the proof and refreshes the member's presence on online Macs before their one-day
drop. A workspace in daily use pays nothing extra for it. Default on; **owner to confirm**.

## 5. Latency

| Path | Warm runtime | Parked runtime |
|---|---|---|
| Mac → Friday, to `delivered` | already connected: engine → peek → turn start ≈ **0.1–0.3 s**, plus the turn | ring 0.1–0.3 s + `WORK#` row → stream → Pipe → FIFO → work Lambda **(unverified ~0.5–1 s)** + cold start **54–57 s measured** + start and restore **~1 s (unverified)** + pull 0.3–1.5 s ≈ **~60 s** |
| Friday → Mac, to `stored` | local socket + relay round trip ≈ **0.2–0.8 s** | n/a (Friday sends from a running turn) |
| Hosted-app send | backend → owning task → node relay → app → engine → peer ≈ **0.3–1 s** | never cold |
| Phone decision | backend → node relay → app → `hold.answer` ≈ **0.2–0.5 s** | never cold |
| Directory, receipt | local, **< 10 ms** | n/a |

The parked case costs the same minute a room message pays today. The stateful bridge answered it in
about 0.1 s. That is the main trade.

## 6. Cost

Fargate arm64 prices: $0.03238 per vCPU-hour and $0.00356 per GB-hour (us-east-1).

| Item | This design | Stateful bridge |
|---|---|---|
| Always-on member compute | **$0** | ≈ $14.40 a month (0.5 vCPU / 1 GiB), more past ~49 workspaces |
| Relay | ours ≈ $15 a month, or n0 $0 | same |
| Secrets Manager | $0 new (a field in the existing boot secret) | $0.40 a month per workspace |
| S3, DynamoDB, NAT data | pennies (KB per network message through the existing NAT) | pennies |
| Extra wakes | microVM time for wakes that would not happen otherwise | none |

Each extra wake costs the cold start plus the time until the early stop (≤ 5 min) or the 30 min
park. **(unverified: AgentCore's per-second rates for idle sessions and the microVM's size.)** The
early stop (4.6) and the "a daily-use workspace needs no keepalive" rule keep the count low. A
ping storm costs at most one microVM kept awake.

## 7. Engine changes (exact, v0.3.0)

Each one is required by a path above. Line references are to v0.2.0.

**E1. `lightbulb-connect member`, the sidecar.**
- Flags: `--home`, `--snapshot`, `--socket`, `--name`, `--relay`, `--issuer`, `--sessions <file>`,
  `--new-key`. The control token comes on stdin.
- It is `embed::start` (`embed.rs`) plus a socket. Each line `{token, id, op, …}` goes to
  `Instance::call` (so `Caller::Instance`; `inbox` takes the cloud form, `ipc.rs:208`).
  `subscribe` streams `Instance::events()`. `stop` and SIGTERM run `Instance::shutdown` (with the
  snapshot), then exit.
- `EmbedOpts.sessions`: declared before `Net::start`. Today `cloud.sessions.set` can only follow
  `start`, so an early network message is refused `policy` (`mail.rs:762`).
- On a new link, the presence frame is written before any outbox envelope (Plan C still-needed
  item 2). Today the presence is built in `spawn_blocking` after `hello` and can race the flush.
- `--new-key` prints the generated member key's public half and seed to the socket's first
  response, for job 4 step 1 only.
- A `superseded {by_peer}` event when a peer closes our link with `SUPERSEDED` (for section 9, risk 10).

**E2. `inbox.peek {session}` → `{messages}` and `inbox.ack {session, ids}` → `{acked}`.**
- `peek` is `mail::waiting` with `ipc::records` → `INBOX_RECORD` (`framed` included).
- `ack` runs `mail::take_one` per id (receipt `delivered`); it never calls `mark_read`.

**E3. `pull {members, quiet_ms, deadline_ms}` → `{connected, received, quiet}`, and
`status.queued_for`.**
- `pull` dials the named members now (not on the 30 s tick). It waits for each one's `hello`,
  then until no envelope has arrived for `quiet_ms`, or the deadline.
- No wire change: a sender flushes all queued envelopes on `hello`, so a quiet window after
  `hello` means drained.
- `queued_for` is the distinct `outbox.to_member` of rows still `queued`.

**E4. Doorbell.**
- A `wake` claim on workspace proofs (`proof.rs`), kept on renewal.
- Ringing on computers as in 4.6: from the flush loop (`queued`) and on home-relay connect
  (`online`), through `http::post_json`, signed with `identity::sign`.
- Logs carry the short key and the reason, never the token.

**E5. `app.local {op: send|inbox|receipt, app, to?, text?, request_id?, reply_to?}` on
computers.** It is the computer half of `app_send.rs` (send as `<short>/<app>-app`; that
session's inbox within `INBOX_BUDGET`; the receipt query). It is called by the desktop app (a
person caller), not by an envelope.

**E6. `hold.answer {id, verdict, scope, decided_by}`.** The decision is applied only when
`decided_by` equals this computer's `person_id`. Scopes are `once | person | own_cloud` with
`decision::judge`'s rules, and it is recorded in `approvals`.

**E7. Member relays off on computers.** A computer no longer sends `hold_notice` to its workspace
member, no longer accepts `decision` frames, and no longer accepts `app_*` envelopes from it.
- Why: the member key is now readable by the workspace's agents (section 9), and the new paths
  (jobs 6 and 7) replace these.
- The code in `decision.rs`/`app_send.rs` stays for E5 and E6. Only the network entry points go.

Not needed: a run-once mode, state import/export (the bridge restores files and uses
`snapshot`/`EmbedOpts.snapshot`), a key-in-memory option, `identity.proof` for instances (the
keepalive keeps renewal inside the grace), or a protocol bump.

## 8. What survives from Plan C

| Plan C task | Fate |
|---|---|
| 1 Pin the engine | survives; `network-version.json` also feeds the Hermes image (a Rust build stage, linux/arm64, smoke test `lightbulb-connect --version`), and `build-hermes-image.yml` adds that path |
| 2 Installer, `/j/`, `/device`, rewrites | survives; add `/connect/wake/*` |
| 3 Issuer (built) | survives; mint and re-mint the `wake` claim; `NETWAKE#` row at connect |
| 4 Relay (step 1 built) | ours or n0 (section 10) |
| 5 Bridge skeleton (built) | replaced. Nothing in `apps/bridge/` runs in the Python bridge. Ported ideas: "put only when the bytes changed" and `slug()`/`sessions()` from `connect.rs` (~20 lines of Python) |
| 6 VPC-only door | gone |
| 7 Connect | backend survives (`NETWORKSPACE`, `NETLINK`, routes, wipes, approval links). The bridge call becomes the two-step `/network/connect` (4.5 job 4); the internal routes become hook-token `/internal/network/{owner}/state`. The bridge half is `bridge/network.py` |
| 8 Send, inbound, routing | sends go through the local socket; inbound runs as local turns (job 2); **the backend room routing is dropped** (section 3) |
| 9 Hosted MCP tools | survive; the transport is the paired Mac (`network.app.v1`, E5) |
| 10 Webhook agents | survive as declared sessions. Their inbound reaches the webhook through a local turn that posts once to the webhook URL from the runtime **(the URL and key then live in that agent's profile `.env`, like other connector secrets)** |
| 11 Holds | backend survives (`NETHOLD`, Needs-you, push, decision route, audit); notices come from the Mac's node event; decisions go to the Mac (E6) |
| 12 Bridge CDK | mostly gone. Kept: the audit table, the backend env, and the boot-secret `PutSecretValue` grant for the per-owner role |
| 13 Hosted MCP gains the tools | survives |
| 14 Friday's tool | survives on the local socket; no `/internal/network/{owner}/sessions|send|receipt` routes |
| 15–17 App | survive; Task 15 adds two commands, one event, and the pairing switch |
| 18–19 Streams | out of scope; they stay on today's desktop relay |
| 20 Phone and web Allow UI | survives whole |
| 21–24 | re-scoped; acceptance adds the parked-runtime path and the doorbell |

## 9. Security

**The member key's exposure.** It sits in the owner's boot secret, which the workspace's agents'
shells can read (4.2), and in the sidecar's home. A prompt-injected agent can take it. With E7,
the key can:
- send as any of the workspace's declared agent sessions and read their network messages, which
  the agents can already do through the tool;
- run an impostor engine outside AWS.

With E7, the key cannot relay decisions, cannot act as a person (`app_*`), and cannot receive hold
notices. It cannot admit, remove or govern: the member never could (ruling §1).

**Risk 10 (impostor engine), accepted with detection and revocation:**
- Effect: peers keep one link per key, so the impostor and the runtime supersede each other on
  every redial. That takes the workspace off the network intermittently, and the impostor sees
  the network messages addressed to the workspace's agents.
- Detection: the sidecar's `superseded` event (E1). The bridge counts it, and more than 3 in
  10 min with no split session reported raises `network.member_key_contested` (page tier).
- Revocation: the runtime runs `team.leave` (a signed leave; removal wins on replay), so the stolen
  key is no longer a member anywhere. Then a reconnect with a fresh key, which needs an admin's
  Allow on a Mac.
- **Owner to confirm** this acceptance. The alternative, a key that the agents cannot read, needs
  the sidecar to run under a different uid with its own credentials. AgentCore gives a runtime
  one execution role, so I see no way to do that in this design.

**Inside one workspace, agents are one principal.** The socket needs the control token, which is
held only in bridge memory, so an agent's shell cannot call the engine directly. With the key it
can still run its own engine (above).

**The state object** is under `<owner>/*`, which the runtime can write. Roster entries are signed
and replayed, so an agent cannot forge membership. Anything else it could inject gives it no
more than sending does.

**The doorbell carries no authority.** It can only cause a pull. The token is visible to every
network member, and the rate limits bound the cost. The engine rings only its own issuer's origin.

**Hosted apps** still act only through the person's own Mac, now over the device-JWT node relay
(`machine_auth.py`, EdDSA paired-device tokens, ≤ 300 s), sending as `<mac short>/<app>-app`.

**Phone decisions** carry `decided_by` = the Clerk user of the tap. The backend checks it (the
`NETHOLD` row under that user), and the engine checks it again (E6, equal to its `person_id`).

**Gating for jobs 6 and 7.** They do not use the selected-Mac execution gate
(`DESKTOP_EXECUTION_ENABLED` plus the single-owner allowlist). That gate exists for
running shell and browser work on a Mac. These jobs send text the person's own app wrote, or the
person's own tap. They are gated by a new **Network** switch in the pairing dialog (consent
once, at pairing), default on, shown and editable in Settings → Desktop. **Owner to confirm**
both: no execution gate (Q4), and a separate switch (Q5). Macs paired before the switch get it
through the signed update's automatic `/desktop/pair` refresh. Until then they answer with the
update message in job 6.

**Compared with the stateful bridge:** there is no long-lived process holding every workspace's
key, and no VPC door. There is one new public route, which carries no content. The member key is
now readable by that workspace's own agents, which E7 limits to their own sessions, and the
impostor risk has detection and revocation.

## 10. Open decisions for the owner

1. **Relay: ours or n0's.** Ours: built on dev (Task 4 step 1), ≈ $15 a month, our logs, our
   limits, and the 90-day `relays.json` re-signing. n0's public relays: $0, no ops, their limits
   and uptime, and the metadata (who talks to whom, when) seen by a third party. Default: ours.
2. **Doorbell: HTTPS token URL or the Mac's paired channel.** Token (default): every member can
   ring; one public route. Paired channel: no public route, but only this workspace's paired apps
   ring, so replies from other workspaces wait for the next wake.
3. The remaining **owner to confirm** items are listed at the end.

## 11. Plan C″ task list

1. Engine v0.3.0, E1: the `member` sidecar, sessions before start, presence first, `superseded` event, `--new-key`.
2. Engine: E2 `inbox.peek`/`inbox.ack`; E3 `pull` and `status.queued_for`.
3. Engine: E4 `wake` claim and ringing.
4. Engine: E5 `app.local`, E6 `hold.answer` with `decided_by`, E7 member relays off.
5. Pin v0.3.0; Rust build stage in the Hermes Dockerfile; `build-hermes-image.yml` path filter.
6. Issuer: the `wake` claim; `NETWAKE#` rows; token rotation on connect.
7. IAM: `PutSecretValue` on the owner's own boot secret for the per-owner role.
8. Bridge `network.py`: start trigger and lock, restore, sweeps, event-driven save with conditional puts, 412, the SIGTERM stop thread, `network.busy()`.
9. Bridge handlers: `/network/connect` (two steps), `/network/disconnect`, `/network/pull`, `/network/sessions`.
10. Bridge: the `network_message` run-less ingress, ack at turn end, the reply on the network.
11. Plugin: `lightbulb_sessions` on the local socket, with the sender from the run's agent.
12. Backend: the connect flow (two steps, `sessions.json`, proof mint), the state route, the keepalive.
13. Backend: the doorbell route, its checks and rate limits, the `WORK#NETWAKE#` row, the `deserialize_pointer` branch and its consumer.
14. Webhook agents as declared sessions (Task 10 reshaped).
15. Desktop app: `network.app.v1`, `network.hold.answer.v1`, `network.hold.v1`, the Network pairing switch.
16. Backend: hosted MCP tools over the paired Mac (Tasks 9 and 13 reshaped).
17. Backend: holds from the Mac's event and decisions to the Mac (Task 11 reshaped); Task 20 UI.
18. Apply the relay decision.
19. `hermes-version.json` pin flip (dev, then prod) and `roll_hermes_runtimes.py`.
20. Acceptance: the parked-runtime inbound, the doorbell, an offline Mac, a hosted app through the Mac, a phone Allow, the 7 h recycle, the key-contested alarm.

## Rejected alternatives

**The stateful Fargate bridge (Plan C as written).** Inbound is instant (~0.1 s), and one task
serves ~49 workspaces per GiB. But it is always on, it keeps a second single-writer state per
workspace, and the lease and debounced snapshots that make it single-writer raced in review. It
also needs a VPC-only door back to the backend.

**A Rust Lambda per job.** It needs no always-on compute and costs about $0.00004 a job. But it
rebuilds what the runtime already has: a per-workspace lock, a state download and upload per job,
a pull-completion signal, and a keepalive. One function role could also read every workspace's key.

## Index: builder-check items and where each is resolved

**Questions:**
- Q1 at-least-once (default): E2 peek and ack, ack at turn end (4.5 job 2).
- Q2 receipt meaning: `delivered` when Friday's turn ends; never `read`. **Owner to confirm.**
- Q3 session names: the slug of the name; a rename makes a new address (Plan C's rule). **Owner to confirm.**
- Q4 execution gate: not used; the Network switch instead (section 9). **Owner to confirm.**
- Q5 pairing switch: a new Network switch, default on. **Owner to confirm.**
- Q6 new members' pings: no deadlock, because the membership filter is removed (4.6 step 2).

**Gaps:**
- G1 socket `inbox` form: the sidecar routes every line through `Instance::call` (4.4).
- G2 state post: after every save, when `queued_for` or `proof_exp` changed; the `online` filter fails open (4.4, 4.6).
- G3 busy: `network.busy()` is a third `_ping_status` term, not `turn._busy` (4.4).
- G4 first put: `IfNoneMatch: "*"` (4.4).
- G5 cold start from a pull: synchronous start under a lock, then 202; a fatal-start path (4.4, job 3).
- G6 declared sessions: backend-owned `sessions.json` plus a `/network/sessions` invoke (job 5).
- G7 `decided_by`: the Clerk user id equals `person_id` = proof `sub` (job 7, 4.2).
- G8 `wake` rotation: a new token per connect; the row is deleted on disconnect; the issuer can revoke (4.6).
- G9 early stop: only when the pull received nothing and no turn or invocation came within 5 min (4.6).
- G10 sidecar stop: `stop` and SIGTERM do the same thing (4.4).

**Risks:**
- R1 warm pool: the start trigger is a real `/invocations` only (4.4).
- R2 stop order: `Instance::shutdown` with `EmbedOpts.snapshot`, never the refused `shutdown` op (4.4).
- R3 read loop: E2.
- R4 lossy event: sweeps at start, after a pull, and on lag (4.4, job 3).
- R5 doorbell transport: a `WORK#NETWAKE#` row through the Pipe plus a pointer branch (4.6).
- R6 false `read`: an ack gives `delivered` only (E2).
- R7 stale member filter: removed (4.6).
- R8 shared SIGTERM budget: the network stop runs on its own thread in parallel; worst-case loss is stated (4.4).
- R9 hosted apps off for most owners: no execution gate; the update message until the grant (section 9).
- R10 impostor engine: accepted with detection (`superseded` → alarm) and revocation (`team.leave` plus reconnect). **Owner to confirm.**

## Unverified in this draft

- UDP and direct QUIC from an AgentCore VPC-mode microVM through the NAT gateway. The relay
  fallback is TCP 443, so only the direct path is in question.
- AgentCore's idle-session billing, its rates, and the microVM's size.
- Whether the per-owner role can already write its own boot secret (I believe it cannot).
- How AgentCore delivers SIGTERM to child processes. The design works either way.
- Engine start, restore and stop timings in the microVM; and the latency of the stream → Pipe →
  FIFO → work Lambda hop.
- Whether the engine repo publishes Linux arm64 binaries. If not, the Dockerfile builds from source.
- Whether v0.2.0 already sends presence before envelopes on a new link. E1 assumes it does not.
