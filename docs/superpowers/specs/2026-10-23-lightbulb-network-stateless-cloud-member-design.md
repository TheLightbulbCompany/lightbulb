# Lightbulb Connect: the workspace member inside the AgentCore runtime (design)

Status: revision 3. It replaces the stateful "bridge" in Plan C (Tasks 5 and 6, the bridge half
of 7, and the bridge parts of 8–14). Nothing here is built. Revision 2 resolved every item in
`.superpowers/sdd/2026-10-22-lightbulb-network-plan-c-bridge-app-cutover/design-builder-check.md`;
the index at the end says where each one now stands.

> **Owner-approved shape, 2026-10-24.** The workspace is one more computer on the free
> `lightbulb` network. Its computer is the engine, running as a sidecar inside the workspace's
> AgentCore microVM, with its own identity key (in the owner's boot secret) and its state in the
> owner's S3 prefix (`<owner>/network/`). It founds the workspace's team. Friday and the other
> cloud agents are that computer's declared sessions. Everything else is the engine's existing
> roster, holds, receipts, relays and tools, unchanged.

Words: a **network message** is the engine's store-and-forward message between members (the
engine's code calls it mail: `mail.rs`). This document never means email.

Sources read:
- the engine at `lightbulb-network.nosync` `main` (v0.2.3; line references below are to v0.2.0
  and were spot-checked on v0.2.3): `src/embed.rs`, `src/daemon/{ipc,net_ops}.rs`,
  `src/net/{mod,frames}.rs`, `src/{mail,outbox,decision,app_send,presence,proof,roster,store,policy,holds,ticket,admission}.rs`;
- the Hermes image: `apps/infra/hermes/Dockerfile`, `bridge/{__init__,server,sync,recycle,turn}.py`,
  and `plugins/lightbulb/__init__.py`;
- the backend: `core/services/{openclaw_work,agentcore_runtime,runtime_transport}.py`,
  `core/repositories/messaging_repo.py`, and the built issuer `network_issuer.py` / `routers/network.py`
  (Plan C Task 3, #4299);
- infra: `container-stack.ts`, `network-stack.ts`, `api-stack.ts`, `lambda/messaging-projector/index.py`;
- Plan C, the workspace-member ruling, the built `apps/bridge/` crate, and the Task 5 and 7 reviews.

Claims I could not check in code are marked **(unverified)**. Decisions only the owner can make
are marked **owner to confirm**.

---

## 1. Intent and why

Plan C runs the workspace member as an always-on Fargate task that holds every workspace's key,
with S3 snapshots and a DynamoDB lease. The reviews found that the lease and the snapshots race:
two bridges could run one key (Task 5 HIGH-1), and a stale snapshot could overwrite a newer one
(HIGH-2).

The owner's rule: stateless, cheaper, on AWS. Every workspace already has exactly one stateful,
single-writer, S3-backed, wake-on-invoke compute unit: its AgentCore runtime. The member moves in.

## 2. What must exist in the cloud at all

- **The workspace's computer.** The agents live in the cloud, so their network messages are
  received there. It holds the member key, the outbox and the seen-ids. It founds the team.
- **Connecting a Mac.** Settings shows "Connect your computer" with a `lightbulb join '<ticket>'
  --name <you>` line the member minted for that person. They paste it in a terminal. No device
  pairing, no desktop app, no new auth.
- **Hosted assistants** (ChatGPT, Claude.ai, Grok Bot, through the hosted MCP at the backend)
  send through the member, labelled "<App> via <Person>". They work whether or not the person's
  Mac is on.

There is no channel from the backend to anyone's Mac. The relay is n0's public relays.

## 3. What goes through the backend, and what never does

This is the owner's rule.

| Through the backend | Never through the backend |
|---|---|
| Wake signals, which carry no content (the doorbell, 4.6) | Network message bodies between members |
| Runtime invocations (`InvokeAgentRuntime` envelopes with no network message content) | Hold text (hold notices carry ids, the sender label and the target only) |
| The invite line: a credential for that person, shown once | The roster |
| A hosted app's requests, carrying only the text that hosted app itself sent | Member keys. The member key lives only in the owner's boot secret (4.3) |
| Member hold decisions: hold id, verdict, scope and `decided_by` | Streams (not in phase 1) |

What this means:
- **Inbound network messages to Friday run as local turns inside the runtime** (4.5 job 2). They
  are not posted into a room. Friday decides what to say, where, and to whom. Rooms and the
  network stay disconnected.
- **The engine's state sits at rest in the owner's S3 prefix** (`<owner>/network/`), next to the
  Hermes profiles. Backend code never reads it. The backend task role can read that bucket.
  The owner accepted this.

## 4. The design

### 4.1 Components

| Piece | Where | New or existing |
|---|---|---|
| `lightbulb member`, the sidecar | baked into the Hermes image, started by the bridge | binary `lightbulb` exists (`Cargo.toml`); subcommand is new (E1) |
| Engine state `<owner>/network/{state.db,addrs.json,relays.json,relays.json.minisig}` | `OWNER_STATE_BUCKET` | new objects; existing bucket and `<owner>/*` grant |
| Declared sessions `<owner>/network/sessions.json` | written by the backend | new, backend-owned (like `config.yaml`) |
| Member key `networkKey` | the owner's boot secret | new field, existing secret |
| `bridge/network.py` | the Python bridge | new |
| `lightbulb_sessions` tool | `plugins/lightbulb/`, which calls the sidecar socket in process | reshaped from Plan C Task 14 |
| "Connect your computer" panel | Settings (web) | new |
| Doorbell `POST /api/v1/network/wake/<token>` | backend, the one public route | new |
| `network_wake` `WORK#` row | the messaging table → Pipe → `openclaw-work.fifo` | new kind on the existing path |
| Hook-token route `POST /api/v1/internal/network/{owner}/state` | backend (hook token) | new; carries no content |
| Member holds as Needs-you items | backend (`NETHOLD`, push, decision route) | reshaped from Plan C Task 11 |
| Relay | n0's public relays | existing |

There is no Lambda, no lease, no VPC-only listener, no always-on member process, and nothing
new in the desktop app.

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
- `embed::start` runs a `kind: cloud` instance and opens no socket. On first start it writes a
  solo genesis (`embed::boot`), so the instance founds its own team. `Instance::shutdown` closes
  the endpoint, waits up to 5 s for its tasks, then writes the final snapshot to
  `EmbedOpts.snapshot`, which must be outside the home (`embed.rs`).
- `Instance::call` runs `ipc::op` as `Caller::Instance`. `NOT_ON_AN_INSTANCE` refuses
  `team.invite`, `reload`, `ui.claim` and `shutdown` (`ipc.rs:166`). An instance is not a person
  (`Caller::is_person`, `ipc.rs:159`), so person-only ops (`team.remove`, `team.admin`,
  `team.admit`, open links, every hold op) refuse it (`net_ops.rs` `gate`; `holds.rs` `op`).
- The join line is `lightbulb join '<ticket>' --name NAME` (`ticket.rs`). A once link lasts 24 h.
  A cloud candidate, a person's new computer, and any `ask` link wait for an Allow
  (`admission.rs:312`).
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
- Queued network messages wait in the **sender's** outbox. They are retried only to online
  members, for `TTL_S = 86_400` (`outbox.rs`). On a `hello` the sender flushes all of them
  (`flush(all)`). There is no end-of-queue marker.
- A cloud instance refuses (`policy`) a network message for a session it has not declared
  (`mail.rs:762`). Its inbound rule `policy::cloud_inbound` accepts or refuses and "never holds"
  (`policy.rs:151`). A Mac refuses one from a cloud `from` that is missing from the member's
  stored presence (`mail.rs:725`). A Mac drops a presence after a day unseen (`presence.rs`).
- A hold on a Mac is answered on that Mac in an `osascript` dialog (`dialog.rs`, `holds.rs`).
  `hold.answer {hold, verdict, scope?}` exists and records `by: "local"` (`holds.rs`).
- Today a hosted app's send is carried by the member to one of the person's computers, which
  sends it as `<short>/<app>-app` (`app_send.rs`, ruling §5).
- One key keeps one live link per peer: a newer connection closes the older with `SUPERSEDED`
  (`net/mod.rs:465-482`). The founder cannot `team.leave` (`FOUNDER_MUST_TRANSFER`, `net_ops.rs`).
- `identity.proof` is refused unless the caller is a person (`net_ops.rs:151`). The engine renews
  its own proof at 20 h while it runs (`proof.rs`, `tick`). (Proofs are phase 3 here.)

### 4.3 The member key

- **Where it lives:** only in the owner's boot secret, as `networkKey` (a base64 Ed25519 seed).
  The backend never holds it.
- **Who makes it:** the runtime, at connect (4.5 job 4). The sidecar generates it (`--new-key`),
  and the bridge writes it into the boot secret with a read-modify-write that keeps `hookToken`
  and `gatewayToken`. This needs `secretsmanager:PutSecretValue` on the owner's own boot secret
  ARN for the per-owner role **(unverified: the role does not have it today, as far as I can
  tell)**. **Ruled 2026-10-24: no `PutSecretValue` grant in phase 1.** The backend mints the key into the boot secret at provision time (as it mints the hook tokens) and never reads it back; `--new-key` is phase 3. That fallback passes the key
  through the backend once and breaks the section 3 rule.
- **Reading it:** `bridge/__init__.py` already loads the boot secret at import. `networkKey` is
  kept in module state (never `os.environ`), like `GATEWAY_TOKEN`. On disk it exists only as the
  sidecar's `identity.key` (0600) in the engine home, and it is excluded from every S3 put.
- **Reprovision:** a full `DELETE`/`POST /debug/provision` mints a new boot secret, which loses
  `networkKey`. The workspace shows "Reconnect workspace" (a new team; section 9). Accepted.

### 4.4 Bridge ↔ engine contract (op by op)

The sidecar (E1) is `embed::start` plus a Unix socket. Every line goes to `Instance::call`, so
every op runs as `Caller::Instance`, and `inbox` takes the cloud form (`ipc.rs:208`). Each
request carries `token`, the per-boot control token. The sidecar strips it before `call` and
refuses a line without it. The token lives only in the bridge's memory, so an agent's shell
cannot use the socket even when it can reach the file.

**Start trigger.** The engine starts only inside `handle_plugin`, on a real, authorized
`/invocations` (never at import or at `main()`), and only when `networkKey` is in the boot secret
and `<owner>/network/sessions.json` exists.
- A pooled microVM gets no `/invocations` until AgentCore claims it for the owner's session
  (`server.py:13-14`), and only one microVM per owner is claimed. So two pooled microVMs never
  both start an engine with one key.
- `network.ensure_started()` runs under one process-wide lock. For a `/network/*` path it is
  synchronous (restore and start take about 1 s **(unverified)**, inside the 90 s budget). For
  any other path it starts in a background thread and does not delay the turn.
- If the engine fails to start (for example `lock_dir` finds a leftover lock), it logs, emits
  `network.start_failed`, and retries on the next invocation after 60 s. It never starts a
  second sidecar.

**Start sequence:**

| Step | Bridge does | Engine op / fact (line) | Gap |
|---|---|---|---|
| 1 | `GetObject` `state.db`, `addrs.json`, `relays.json(.minisig)`, `sessions.json`. Keep `state.db`'s ETag, or "absent" | — | — |
| 2 | Write `identity.key` 0600 from `networkKey` into `/opt/data/network/` | engine reads `identity.key` from home (`identity.rs`, `load_or_create`) | — |
| 3 | Spawn `lightbulb member --home /opt/data/network --snapshot /opt/data/network-out/state.db --socket /opt/data/network.sock --name <ws name> --issuer <url> --sessions <sessions.json>`, with the token on stdin | `EmbedOpts {kind: Cloud, relay, issuer, snapshot, name}` (`embed.rs`) | **E1:** the subcommand; `EmbedOpts.sessions`, declared before `Net::start`; presence before envelopes |
| 4 | `subscribe` | `"subscribe" if instance` (`ipc.rs:207`) | **E1:** the sidecar streams `Instance::events()` to a subscribed line |
| 5 | Inbox sweep: `inbox.peek {session}` for every declared session | — | **E2:** `inbox.peek` |

**Running:**

| Need | Op | Line | Notes |
|---|---|---|---|
| Friday sends | `send {from: "<ws short>/<slug>", to, text, request_id, reply_to?}` | `ipc.rs:280` | `from` must be declared (`ipc.rs`, `not_declared`) |
| Receipt | `receipt {from, request_id}` | `ipc.rs:287` | |
| Directory | `directory {query?}` | `ipc.rs:303` | |
| Read inbound without taking | `inbox.peek {session}` → `{messages: [INBOX_RECORD]}` | — | **E2.** `mail::waiting` with `ipc::records` (`ipc.rs:784`), so each record carries `framed` (`ipc.rs:170`) |
| Acknowledge inbound | `inbox.ack {session, ids: [id]}` → `{acked: [id]}` | — | **E2.** `mail::take_one` per id: `delivered`, never `read` (Q2) |
| Re-declare sessions | `cloud.sessions.set {sessions}` | `ipc.rs:209` | after a `/network/sessions` invoke (job 5) |
| Pull | `pull {members: [key], quiet_ms, deadline_ms}` → `{connected, received, quiet}` | — | **E3** |
| What is queued for whom | `status` → `queued_for: [member key]` | `ipc.rs:213` | **E3:** add `queued_for` |
| Mint an invite line | `team.invite {mode: "person", for}` → `{line, expires_at}` | refused today (`ipc.rs:166`) | **E8** |
| Hosted app | `app.send|app.inbox|app.receipt {app, person, …}` | — | **E5** |
| Member holds | `holds`, `hold.get`, `hold.answer {hold, verdict, scope?, decided_by}` | `holds.rs` (person-only today) | **E6** |
| Snapshot | `snapshot {path}` | `net_ops.rs:200-203` (admitted for `Caller::Instance`) | path outside the home |
| Stop | sidecar-level `stop` → `Instance::shutdown` | `embed.rs` | `shutdown` itself is refused (`ipc.rs:166`) |

**Events the bridge acts on:** `message` (→ peek that session), `receipt_changed`, `held` and
`hold_closed` (→ the content-free hold notice, job 7), and `superseded` (**E1**, section 9). On a
`Lagged` gap on the event line, the bridge runs an inbox sweep.

**Save (event-driven):** after any event, and after any `send`, `inbox.ack`, `pull`,
`team.invite`, `app.*` or `hold.answer`, the bridge waits 1 s (debounce). Then it calls
`snapshot {path: /opt/data/network-out/state.db}`, skips the put if the bytes are unchanged
(SHA-256), and puts `state.db` with `IfMatch: <ETag>`, or `IfNoneMatch: "*"` when it was absent
at restore. `addrs.json` and the relay cache are put unconditionally (they are hints). After a
put, if `queued_for` changed since the last post, it posts `/internal/network/{owner}/state
{queued_for, state}` (hook token, no content).

**On 412** (another writer): the bridge sends the sidecar `stop`, does not put again, emits
`network.state_conflict`, and leaves the network alone for this process.

**Stop sequence (SIGTERM, and the 7 h recycle, which also SIGTERMs):**
1. `_save_on_sigterm` starts the network stop on its own thread, in parallel with the profile
   saves, so the profiles keep their full 10 s.
2. The bridge sends the sidecar `stop`. The sidecar treats its own SIGTERM as `stop` too
   **(unverified: how AgentCore delivers SIGTERM to child processes)**.
3. The sidecar runs `Instance::shutdown`: it closes the endpoint, waits ≤ 5 s for tasks, writes
   the final snapshot to `--snapshot`, and exits.
4. The bridge waits for the exit (≤ 6 s), makes the conditional put of `state.db` (≤ ~1 s), then
   posts state if time remains.
5. If the 10 s run out first, the last debounced save stands: at most 1 s plus one put is lost.

There is no stop at the end of a turn. Saves are event-driven.

**Busy:** `network.busy()` is a third term in `_ping_status` (`server.py:772-775`): true while a
pull runs. It is NOT `turn._busy`, so a recycle's `hold_turns` and idle wait are unchanged.

### 4.5 Jobs

**(1) Friday sends** (`lightbulb_sessions` `send`; webhook agents, Task 10).
- The Hermes plugin calls the sidecar's local socket to send, read receipts and list the
  directory. It derives `from` from the turn's agent id through `sessions.json`, not from the
  model's arguments.
- Mac online → `stored`/`held`/`delivered` within about a second. Mac offline → `queued` in the
  member's outbox, saved with the state, and shown in `queued_for`.
- Friday posts to a room only when she chooses to, with the room tools she already has.

**(2) Inbound network messages → a local turn (at-least-once).**
- A Mac sends to `friday` like any session. The message waits on the sender's Mac until the
  member takes it.
- Trigger: a `message` event, a start sweep, a post-pull sweep, or a `Lagged` sweep. The bridge
  calls `inbox.peek {session}` and starts one turn per record through a new run-less ingress,
  `network_message` (like agent-mail ingress: no `run` block), with
  `eventId = <network message id>`, `targetAgentId` from `sessions.json`, and the `framed` text.
- `turn.register_run("event:<id>")` dedupes within the process. When the turn ends (any
  outcome), the bridge calls `inbox.ack`, and the receipt moves to `delivered`.
- If the microVM dies mid-turn, the record is still waiting and the next start runs it again.
  At most one repeat per crash is accepted.
- Friday decides what to say, where and to whom, with her tools. Nothing is posted to the
  backend except usage counts on `/internal/messaging/usage/{ownerId}` (no content).
- Spend stays behind the openrouter-gate. Loops stay bounded by the engine's hop and rate limits
  (`mail.rs`: `MAX_HOPS = 4`, `LOOP_LIMIT = 12` per `LOOP_WINDOW_S = 600`).

**(3) Pull after a doorbell.** `InvokeAgentRuntime {path: "/network/pull", body: {from, reason}}`.
- The bridge runs `ensure_started()` synchronously, then a sweep, and answers 202.
- On a thread: `pull {members: [from], quiet_ms: 1500, deadline_ms: 20000}`, a sweep, a save.
- A second wake during a start waits on the same lock. A second pull simply runs: `pull` is
  idempotent.

**(4) Connect a workspace, and a computer.**
- Connect, step 1: the backend invokes `/network/connect`. The bridge makes a fresh home, starts
  the sidecar with `--new-key`, writes `networkKey` into the boot secret, and answers
  `{member_key}`. The first start writes the solo genesis: the workspace is the founder.
- Step 2: the backend writes `sessions.json`, mints the doorbell token, writes its `NETWAKE#`
  row, and invokes `/network/sessions`.
- Connect a computer: Settings → "Connect your computer". The person's signed-in web session
  calls a Clerk route; the backend invokes `/network/invite {for: <Clerk user id>, name}`. The
  bridge calls `team.invite` (E8) and returns the line, which the page shows once.
- The person pastes it in a terminal. The Mac joins through the member's live link, no Allow
  needed (E8). The line carries the doorbell token (E4).
- Disconnect: `/network/disconnect` → `stop`. The backend removes `networkKey`, deletes
  `<owner>/network/*`, the `NETWAKE#` row and the owner's network rows. The founder cannot
  `team.leave`, so the team ends with it.

**(5) Session list changes.** When the roster's agents, the webhook agents or the hosted-app
sessions change, the backend rewrites `sessions.json` and invokes `/network/sessions`. The bridge
calls `cloud.sessions.set`. A parked runtime picks up the change at its next start.

Names follow Plan C's rule: `<slug of the display name>`, and a rename makes a new address. A
network message to the old name is refused `policy`. **Ruled 2026-10-24 (Q3): as written.**

**(6) Hosted assistants through the member.**
- `send_network`, `check_inbox` and `message_receipt` (Tasks 9 and 13) go: hosted MCP → backend
  → `InvokeAgentRuntime /network/app {op, app, person, to?, text?, request_id?, reply_to?}` →
  `app.*` (E5).
- The member sends from a declared session for that app and person, labelled
  "<App> via <Person>". The recipient holds it like any member's first message **(unverified for
  a cloud sender: it depends on the recipient's `policy::decide`)**.
- It works whether or not the person's Mac is on. A parked runtime adds the cold start.

**(7) Holds.**
- A hold on a Mac is answered on that Mac, in the dialog shipped in 0.2.x. Nothing else.
- A hold on the workspace member (E6) shows on the web and phone as a Needs-you item. The bridge
  turns `held`/`hold_closed` into a hook-token notice `{hold_id, kind, from, label,
  target_session, created_at}` (no text). `NETHOLD` and the push follow (Task 11).
- A tap → `POST /api/v1/network/holds/{id}/decision` (Clerk) → `InvokeAgentRuntime
  /network/hold {hold_id, verdict, scope, decided_by}` → `hold.answer` (E6).

### 4.6 The doorbell

Semantics: **"connect to me now"**, not "you have a message". The member connects to the member
that rang, takes what it flushes, and flushes its own outbox to it.

**Who rings (E4, on computers):** a computer rings only after a dial to the cloud member failed.
- `queued`: it has outbox rows due for that member. It rings at once, then backs off from 30 s,
  doubling up to 15 min, while rows are due.
- `online`: its home relay connection came up and the dial failed. At most once per cloud member
  per 10 min.

**Where it rings:** `POST {issuer}/wake/<token>`, that is,
`https://thelightbulb.company/connect/wake/<token>`, which the issuer rewrite (Task 2) maps to
`/api/v1/network/wake/<token>`.
- The token is 256 random bits, base64url, one per workspace. It is carried in the invite line
  (E4). The engine builds the URL from its own configured issuer, so no member can aim pings at
  another host.

**The ping:** JSON, 3 s timeout, no retry beyond the backoff.
```json
{"v": 1, "from": "<member key>", "to": "<cloud member key>",
 "reason": "queued" | "online", "at": 1793000000, "sig": "<Ed25519 over the canonical JSON without sig>"}
```

**The backend:** a known token answers `202 {}`; a bad token is a 4xx and nothing else.
1. `GetItem NETWAKE#<sha256(token)>` → owner. None → 4xx, stop.
2. Drop the ping unless `to` equals the workspace's member key, `|now − at| ≤ 300`, and the
   signature verifies against `from`. There is **no** membership filter: it could not be fresh
   while the runtime is parked, the token limits who knows the URL, and the rate limits bound the
   cost.
3. `reason = online` and `from` is not in the last posted `queued_for` → stop. This filter fails
   open: a missing post means "wake".
4. Rate limits: one accepted ping per (owner, from) per 5 s; one wake per owner per 10 s
   (conditional update on `NETWORKSPACE.last_wake_at`).
5. Write the `WORK#` row (it rides the Pipe; no direct `SendMessage`, so no second event bus):

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
   `{kind, ownerId, from, reason}`, `from` 52 lowercase base32 characters, `reason` one of the
   two values. The consumer invokes `/network/pull`. The projector ignores the row (`index.py:1124`).

**Token life:** a reconnect mints a new token and deletes the old row; a disconnect deletes the
row. A ping with a dead token stops at step 1. Lines minted before a rotation carry the dead
token. **Ruled 2026-10-24: the current token travels in the member's roster entry, so a rotation is a new entry and nobody re-joins; an invite line carries the token only until the join.**

**Early stop after a network-only wake:** the bridge asks for its usual recycle stop
(`runtime/{OWNER}/recycle`) only if the pull received nothing, no turn ran, and no other
invocation arrived within 5 min. So a local turn never gets a 503.

### 4.7 Two writers

- One claimed microVM per owner, and the engine's own `daemon.lock`, keep a single writer.
- Every `state.db` put is conditional (4.4). On a 412 the loser stops.
- If a split session ever runs two sidecars, peers keep one link per key (`SUPERSEDED`), and
  receivers dedupe by envelope id (`seen_ids`). Detection: `docs/runbooks/runtime-split-brain-detection.md`.
- A save cut off mid-put leaves the previous object: one S3 put replaces one object atomically.

### 4.8 The state object

`state.db` is a `VACUUM INTO` copy, read once per microVM process and written after changes.

| Table | Holds | Size, small workspace |
|---|---|---|
| `roster` | signed entries | ~0.5 KB per entry (proof entries, ~1.5 KB a day each, start in phase 3) |
| `messages` | every network message sent or received (≤ 64 KiB) | ~1 KB each; **never pruned** (no code deletes these rows outside a test) |
| `receipts` | one per network message | ~0.1 KB each; never pruned |
| `outbox`, `seen_ids` | queued sends and dedupe, 8 days | small |
| `peers_sessions` | the last presence of each member | 1–30 KB per member |
| `holds`, `approvals` | the member's holds and their decisions | small |

Measured: a developer Mac's `state.db` with 11 roster entries and 17 messages is 147 KB. A restore
or a put is tens of ms, once per process. Growth (pruning) is Plan D.

Re-deriving the state from peers was rejected. A fresh engine writes a new solo genesis
(`embed::boot`), and the outbox, `seen_ids` and own-send receipts exist nowhere else.

### 4.9 Failure modes

| Case | What happens | What the person sees |
|---|---|---|
| Parked runtime; a Mac sends to Friday | the dial fails; the Mac rings `queued`; cold start; pull; local turn | `queued`, then `delivered` in about a minute |
| Doorbell lost | the Mac re-rings with backoff up to 15 min; any later wake also connects | longer `queued`; after 24 h `expired` |
| Friday sends; Mac offline | waits in the member's outbox; `queued_for` posted | `queued`; when the Mac comes online its dial fails, it rings `online`, the runtime wakes and flushes |
| Park or recycle with messages queued | the SIGTERM stop sequence saves them | nothing, unless no wake comes within 24 h |
| Hard kill without SIGTERM | changes since the last save are lost; an un-acked inbound one comes back on the next start | at worst one network message lost or run twice |
| 412 on put | that sidecar stops | nothing; a metric |
| n0 relays down | members with no direct path reach no one; everything queues | `queued`; stale directory |
| Duplicate pings | dedupe id, rate limits, stale `at`; `pull` is idempotent | nothing |
| Ping storm | ≤ 1 wake per owner per 10 s | nothing; cost in section 6 |
| Bad doorbell token | 4xx | nothing |
| Network message to an undeclared or renamed session | refused `policy` | the sender sees `refused` |
| Presence dropped on a Mac (a day unseen) | E1 sends presence before any envelope on a new link | nothing |
| Invite line not used within 24 h | the once link expires (`ticket.rs`) | "Connect your computer" again |
| Hosted app; parked runtime | cold start first | the send takes about a minute |

### 4.10 Keepalive

Phase 1 needs none. There is no proof to renew, and E1 sends presence first on every new link,
so a Mac's one-day presence drop does not block the member. With verified names (phase 3) the
backend's scheduled worker writes a `network_wake` row with `reason: "keepalive"` for a workspace
whose last state post is older than 20 h, to renew the proof inside the issuer's grace.

## 5. Latency

| Path | Warm runtime | Parked runtime |
|---|---|---|
| Mac → Friday, to `delivered` | already connected: engine → peek → turn start ≈ **0.1–0.3 s**, under a second, plus the turn | failed dial + ring + `WORK#` row → stream → Pipe → FIFO → work Lambda **(unverified ~0.5–1 s)** + cold start **54–57 s measured** + start and restore **~1 s (unverified)** + pull 0.3–1.5 s ≈ **~60 s** |
| Friday → Mac, to `stored` | local socket + relay round trip ≈ **0.2–0.8 s** | n/a (Friday sends from a running turn) |
| Hosted-app send | backend → invoke → sidecar → peer **(unverified ~0.3–1 s)** | ≈ **~60 s** |
| Member hold decision | backend → invoke → `hold.answer` **(unverified ~0.2–0.5 s)** | ≈ **~60 s** |
| Directory, receipt | local, **< 10 ms** | n/a |

The parked case costs the same minute a room message pays today. The stateful bridge answered it
in about 0.1 s. That is the main trade.

## 6. Cost

Fargate arm64 prices: $0.03238 per vCPU-hour and $0.00356 per GB-hour (us-east-1).

| Item | This design | Stateful bridge |
|---|---|---|
| Always-on member compute | **$0** | ≈ $14.40 a month (0.5 vCPU / 1 GiB), more past ~49 workspaces |
| Relay | n0 public relays, $0 | ours ≈ $15 a month |
| Secrets Manager | $0 new (a field in the existing boot secret) | $0.40 a month per workspace |
| S3, DynamoDB, NAT data | pennies (KB per network message through the existing NAT) | pennies |
| Extra wakes | microVM time for wakes that would not happen otherwise | none |

Each extra wake costs the cold start plus the time until the early stop (≤ 5 min) or the 30 min
park. **(unverified: AgentCore's per-second rates for idle sessions and the microVM's size.)** A
ping storm costs at most one microVM kept awake.

## 7. Engine changes (exact, v0.3.0)

Each one is required by a path above.

**E1. `lightbulb member`, the headless sidecar.**
- Flags: `--home`, `--snapshot`, `--socket`, `--name`, `--relay`, `--issuer`, `--sessions <file>`,
  `--new-key`. The control token comes on stdin.
- It is `embed::start` plus a socket. Each line `{token, id, op, …}` goes to `Instance::call`.
  `subscribe` streams `Instance::events()`. `stop` and SIGTERM run `Instance::shutdown` (with the
  snapshot), then exit.
- `EmbedOpts.sessions`: declared before `Net::start`. Today `cloud.sessions.set` can only follow
  `start`, so an early network message is refused `policy` (`mail.rs:762`).
- On a new link, the presence frame goes before any outbox envelope. Today it is built in
  `spawn_blocking` after `hello` and can race the flush.
- `--new-key` generates `identity.key` and returns the public half and seed in the socket's first
  response, for job 4 step 1 only.
- A `superseded {by_peer}` event when a peer closes our link with `SUPERSEDED` (section 9).

**E2. `inbox.peek {session}` and `inbox.ack {session, ids}`.** `peek` is `mail::waiting` with
`ipc::records` (`framed` included). `ack` runs `mail::take_one` per id (`delivered`); it never
calls `mark_read`.

**E3. `pull {members, quiet_ms, deadline_ms}` and `status.queued_for`.**
- `pull` dials the named members now, waits for each one's `hello`, then until no envelope has
  arrived for `quiet_ms`, or the deadline. No wire change: a sender flushes all on `hello`.
- `queued_for` is the distinct `outbox.to_member` of rows still `queued`.

**E4. The doorbell, rung after a failed dial.**
- Tickets the member mints carry `wake` (the token). A computer that joined through one keeps it
  for that cloud member.
- A computer rings as in 4.6 when a dial to that member fails, through `http::post_json`, signed
  with `identity::sign`. Logs carry the short key and the reason, never the token.

**E5. `app.send|app.inbox|app.receipt {app, person, …}` on the member.** It sends from the
declared session for (app, person), rendered "<App> via <Person>" (`label.rs`), and reads that
session's inbox within `INBOX_BUDGET`. The relay to a person's computer (`app_send.rs`) is
retired.

**E6. Holds on the member.**
- Where `policy::cloud_inbound` refuses today, the member holds instead. **Ruled 2026-10-24: the member accepts the workspace's own people (their Macs joined from a Settings-minted line, which records who it was for) and holds a first message from anyone else.** (The text below was: which
  senders hold, and which stay refused)**.
- `holds`, `hold.get` and `hold.answer {hold, verdict, scope?, decided_by}` are admitted for
  `Caller::Instance`. `decided_by` (the Clerk user id) is recorded in `approvals` in place of
  `by: "local"`. Scopes follow `decision::judge`.

**E7. Member relays off on computers.** A computer no longer sends `hold_notice` to its
workspace member, no longer accepts `decision` frames, and no longer accepts `app_*` envelopes
from it. The member key is readable by the workspace's agents (section 9), and nothing uses
these paths now.

**E8. Invites on the member.** `team.invite {mode: "person", for}` is admitted for
`Caller::Instance` (today `NOT_ON_AN_INSTANCE`). It mints a once link whose join is admitted
without an Allow (today a person's new computer waits, `admission.rs:312`): the backend's
authenticated request for that person is the Allow.

Not needed: a run-once mode, state import/export, a key-in-memory option, `identity.proof` for
instances, or a protocol bump **(unverified for E4's ticket field and E5's label)**.

## 8. What survives from Plan C

| Plan C task | Fate |
|---|---|
| 1 Pin the engine | survives; `network-version.json` also feeds the Hermes image (a Rust build stage, linux/arm64, smoke test `lightbulb version`), and `build-hermes-image.yml` adds that path |
| 2 Installer, `/j/`, `/device`, rewrites | survives; add `/connect/wake/*` |
| 3 Issuer (built) | phase 3 (verified names). Phase 1 uses only its origin for the doorbell |
| 4 Relay (step 1 built) | not used; n0's public relays. Ours only if customers need it |
| 5 Bridge skeleton (built) | replaced. Ported ideas: "put only when the bytes changed" and `slug()`/`sessions()` from `connect.rs` (~20 lines of Python) |
| 6 VPC-only door | gone |
| 7 Connect | backend survives (`NETWORKSPACE`, `NETLINK`, routes, wipes). The bridge call becomes `/network/connect` plus `/network/invite` (job 4); the internal route is hook-token `/internal/network/{owner}/state` |
| 8 Send, inbound, routing | sends go through the local socket; inbound runs as local turns; **room routing is dropped** |
| 9, 13 Hosted MCP tools | survive; they send through the member (job 6, E5) |
| 10 Webhook agents | survive as declared sessions. Inbound reaches the webhook through a local turn that posts once from the runtime **(the URL and key live in that agent's profile `.env`, like other connector secrets)** |
| 11 Holds | backend survives (`NETHOLD`, Needs-you, push, decision route, audit), for member holds only |
| 12 Bridge CDK | mostly gone. Kept: the audit table, the backend env, and the boot-secret `PutSecretValue` grant |
| 14 Friday's tool | survives on the local socket |
| 15–17 App | gone; nothing new in the desktop app |
| 18–19 Streams | phase 2 |
| 20 Phone and web Allow UI | survives for member holds; Mac holds on the phone are phase 3 |
| 21–24 | re-scoped; acceptance adds the parked-runtime path and the doorbell |

## 9. Security

**The member key's exposure.** It sits in the owner's boot secret, which the workspace's agents'
shells can read (4.2), and in the sidecar's home. A prompt-injected agent can take it.

**The key is now the founder's key.** That is new, and the main risk. With it, an impostor
engine outside AWS can:
- send as any declared session and read their network messages;
- act as the founder: mint links, admit, remove and make admins, because at its own socket it is
  a person (`Caller::is_person`).

E7 still stops it from relaying decisions or acting as a person through a Mac. **Owner to
confirm** this acceptance. A key the agents cannot read needs the sidecar under a different uid
with its own credentials, and AgentCore gives a runtime one execution role.

**Risk 10 (impostor engine): detection and revocation.**
- Effect: peers keep one link per key, so the impostor and the runtime supersede each other on
  every redial.
- Detection: the `superseded` event (E1). More than 3 in 10 min with no split session reported
  raises `network.member_key_contested` (page tier).
- Revocation: the founder cannot `team.leave` (`FOUNDER_MUST_TRANSFER`). So revocation is
  "Reconnect workspace": a new key, a new team, and every Mac pastes a new line.

**Inside one workspace, agents are one principal.** The socket needs the control token, held only
in bridge memory, so an agent's shell cannot call the engine directly. With the key it can still
run its own engine (above).

**The state object** is under `<owner>/*`, which the runtime can write. Roster entries are signed
and replayed, so an agent cannot forge membership without the key.

**The doorbell is the one public surface.** It needs the token, it is rate-limited, and it can
only cause a pull. A bad token is a 4xx and nothing else. The engine rings only its own issuer's
origin. The token is known to everyone who got an invite line.

**The invite line** is a credential for that person, shown once over their signed-in session. It
admits one computer within 24 h without an Allow (E8).

**Member hold decisions** carry `decided_by` = the Clerk user of the tap. The backend checks the
user against the `NETHOLD` row, and the engine records it (E6).

**Compared with the stateful bridge:** no long-lived process holds every workspace's key, and
there is no VPC door and no channel from the backend to a Mac. The member key is readable by that
workspace's own agents, and it now carries founder authority.

## 10. Decided

- **Relay:** n0's public relays. Ours later, only if customers need it.
- **Doorbell:** the HTTPS token URL.
- **Inbound to Friday:** a local turn, not posted into rooms.
- **State at rest** in the owner's S3 prefix: accepted.
- **Streams:** not in phase 1.

Phases:
1. Everything in this document.
2. Session streams to the web and phone, through the member, to AppSync.
3. Phone approval of holds that sit on a Mac; verified names (the issuer, the key ceremony).

Rulings 2026-10-24 (controller; the owner may overrule any): no `PutSecretValue` grant in phase 1 (4.3); Q2 and Q3 as marked; the member accepts the workspace's own people and holds everyone else's first message (E6); the doorbell token lives in the roster entry, rotation needs no re-join (4.6); the workspace founds and the founder-key residual risk is accepted for phase 1 with the key passed over a pipe and "Reset the network" as revocation (section 9).

## 11. Plan C″ task list (phase 1)

1. Engine v0.3.0, E1: the `member` sidecar, sessions before start, presence first, `superseded`, `--new-key`.
2. Engine: E2 `inbox.peek`/`inbox.ack`; E3 `pull` and `status.queued_for`.
3. Engine: E4 the ticket `wake` field and ringing after a failed dial.
4. Engine: E5 `app.*` on the member, E6 member holds with `decided_by`, E7 member relays off, E8 invites on the member.
5. Pin v0.3.0; Rust build stage in the Hermes Dockerfile; `build-hermes-image.yml` path filter.
6. IAM: `PutSecretValue` on the owner's own boot secret for the per-owner role.
7. Bridge `network.py`: start trigger and lock, restore, sweeps, event-driven save with conditional puts, 412, the SIGTERM stop thread, `network.busy()`.
8. Bridge handlers: `/network/connect`, `/network/invite`, `/network/disconnect`, `/network/pull`, `/network/sessions`, `/network/app`, `/network/hold`.
9. Bridge: the `network_message` run-less ingress, ack at turn end.
10. Plugin: `lightbulb_sessions` on the local socket, with the sender from the run's agent.
11. Backend: the connect flow (`sessions.json`, doorbell token, `NETWAKE#` row), the invite route, the state route.
12. Backend: the doorbell route, its checks and rate limits, the `WORK#NETWAKE#` row, the `deserialize_pointer` branch and its consumer.
13. Webhook agents as declared sessions (Task 10 reshaped).
14. Web: the Settings "Connect your computer" panel (mint, show once, copy).
15. Backend and web: member holds as Needs-you items (notice route, `NETHOLD`, push, decision → `/network/hold`; Tasks 11 and 20 reshaped).
16. Backend: hosted MCP tools send through the member (Tasks 9 and 13 reshaped).
17. `hermes-version.json` pin flip (dev, then prod) and `roll_hermes_runtimes.py`.
18. Acceptance: a pasted line, the parked-runtime inbound, the doorbell, an offline Mac, a hosted app with the Mac off, a member hold decided on the phone, the 7 h recycle, the key-contested alarm.

## Rejected alternatives

**The stateful Fargate bridge (Plan C as written).** Inbound is instant (~0.1 s), and one task
serves ~49 workspaces per GiB. But it is always on, it keeps a second single-writer state per
workspace, and its lease and debounced snapshots raced in review. It also needs a VPC-only door.

**A Rust Lambda per job.** No always-on compute, about $0.00004 a job. But it rebuilds what the
runtime has: a per-workspace lock, a state download and upload per job, a pull-completion signal.
One function role could also read every workspace's key.

**Hosted apps through the person's paired Mac (revision 2).** It needed new desktop-app commands
and a pairing switch, and it failed whenever the Mac was off. The member sends for them instead.

**A Mac↔backend channel for phone approval of Mac holds (revision 2).** It put a backend channel
into every Mac and new commands into the desktop app. Mac holds stay on the Mac; phone approval
of them is phase 3.

## Index: builder-check items and where each is resolved

**Questions:**
- Q1 at-least-once (default): E2 peek and ack, ack at turn end (job 2).
- Q2 receipt meaning. **Ruled 2026-10-24:** `delivered` when the member hands the message to Friday's turn (turn start), `read` when that turn ends — the engine's own meanings, unchanged.
- Q3 session names: the slug of the name; a rename makes a new address. **Ruled 2026-10-24: as written.**
- Q4 execution gate, Q5 pairing switch: gone with the paired-Mac channel.
- Q6 new members' pings: no deadlock; there is no membership filter (4.6 step 2).

**Gaps:**
- G1 socket `inbox` form: every line goes through `Instance::call` (4.4).
- G2 state post: after a save when `queued_for` changed; the `online` filter fails open (4.4, 4.6).
- G3 busy: `network.busy()` is a third `_ping_status` term, not `turn._busy` (4.4).
- G4 first put: `IfNoneMatch: "*"` (4.4).
- G5 cold start from a pull: synchronous start under a lock, then 202; a fatal-start path (4.4, job 3).
- G6 declared sessions: backend-owned `sessions.json` plus a `/network/sessions` invoke (job 5).
- G7 `decided_by`: recorded on the member for member holds (E6); Mac holds stay on the Mac.
- G8 token rotation: a new token per reconnect; the row is deleted on disconnect (4.6).
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
- R8 shared SIGTERM budget: the network stop runs on its own thread; worst-case loss is stated (4.4).
- R9 hosted apps off for most owners: gone; they send through the member (job 6).
- R10 impostor engine: detection and revocation; now with founder authority (section 9). **Ruled 2026-10-24, owner may overrule:** the workspace founds (a person's Mac as founder breaks onboarding); the bridge hands the key to the sidecar over a pipe at start, never a file or env in the microVM, and no tool exposes it; residual risk accepted for phase 1: an agent with a shell in the same microVM could still extract it from the process. Revocation is "Reset the network" in Settings (new key, new team, Macs re-join). A separate Unix user for the sidecar is the phase-3 hardening.

## Unverified in this draft

- UDP and direct QUIC from an AgentCore VPC-mode microVM through the NAT gateway. The relay
  fallback is TCP 443, so only the direct path is in question.
- AgentCore's idle-session billing, its rates, and the microVM's size.
- Whether the per-owner role can already write its own boot secret (I believe it cannot).
- How AgentCore delivers SIGTERM to child processes. The design works either way.
- Engine start, restore and stop timings in the microVM; the stream → Pipe → FIFO → work Lambda
  hop; the warm hosted-app and hold-decision paths.
- Whether a recipient holds a hosted app's first message from a cloud sender (job 6).
- Whether E4's ticket field and E5's label need a protocol bump.
- Whether the engine repo publishes Linux arm64 binaries. If not, the Dockerfile builds from source.
- Whether v0.2.3 already sends presence before envelopes on a new link. E1 assumes it does not.
