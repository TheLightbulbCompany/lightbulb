# lightbulb-network: architecture and contracts

Status: Phase 2 artifact, 2026-10-22, revised after the builder review the same day. Binding over the four part
specs and Plan A where they differ; those are reconciled to this document. "Owner" marks rulings the owner made.

## 1. The three parts and what crosses between them

```
                 ┌──────────────────────────────────────────────────────────┐
  public repo    │  ENGINE  lightbulb-network (one signed binary per computer) │
                 │  identity · membership · transport · directory · delivery │
                 │  holds · receipts · streams · CLI · MCP server · hooks     │
                 └───────▲───────────────────────────────▲───────────────────┘
                         │ IPC (unix socket, snake_case JSON)│ engine wire protocol (QUIC, e2e)
  private monorepo  ┌────┴──────────┐              ┌────────┴──────────────────┐
                    │ APP (Tauri)   │              │ BRIDGE (Rust service,     │
                    │ renders, asks │              │ engine as a crate, many   │
                    │ ships engine  │              │ member identities)        │
                    └───────────────┘              └──────────▲────────────────┘
                                                              │ internal HTTP
                                                   ┌──────────┴────────────────┐
                                                   │ BACKEND (FastAPI): Friday │
                                                   │ runs, hosted MCP, phone   │
                                                   │ Allow, proofs, link rows  │
                                                   └───────────────────────────┘
```

One direction of dependency: app and bridge depend on the engine crate pinned by release tag; the engine
depends on nothing of ours. The engine has exactly one Lightbulb-specific default, the sign-in issuer URL
(`config.toml: issuer`, default `https://thelightbulb.company/network`), configurable like a relay.

## 2. Artefacts, names, paths

| Thing | Ruling |
|---|---|
| Engine binary | `lightbulb-network`, a signed and notarized bare Mach-O (arm64), at the one canonical path `~/.lightbulb/network/bin/lightbulb-network`. The installer, the app and (later) brew all write that path. Hooks, MCP entries and the launchd job point at it. |
| The `lightbulb` command | Always the engine: `~/.local/bin/lightbulb` → the canonical path. Subcommands the engine does not own (`claude`, `codex`, `share`, `continue`: the app's session launchers) are forwarded to the installed app's binary; without the app they print "needs the Lightbulb app: thelightbulb.company/download". |
| Install line | `curl -fsSL https://thelightbulb.company/install \| sh` (the existing URL; it installs the engine, not the app). The app is a separate download. |
| Join line | `Join the Lightbulb network: https://thelightbulb.company/j/#<ticket>`. The ticket is in the URL **fragment**, so it never reaches our servers or logs; the `/j/` page is static text (the install line and `lightbulb join '<link>'`) and knows nothing about the team. `lightbulb://join/<ticket>` is the same ticket for the app. |
| State | `~/.lightbulb/network/` (0700): `sock`, `state.db` (`PRAGMA user_version`, migrations forward only), `identity.key` (0600; moved into the keychain where one exists), `config.toml`, `relays.json`, `daemon.lock`, `inbox/<id>/` (files), `app-install.json` (present when the app manages the engine). The current team link secret is stored in plaintext in `state.db` (`team.link_secret`), under the 0700 folder next to `identity.key`, so `invite` can reprint the link; single-use `--once` tickets are stored hashed and deleted on use. |
| Updates | The engine self-updates from the public repo's signed releases (minisign; two public keys compiled in; the private key lives only in the owner's 600 file and the public repo's secrets). When `app-install.json` exists, self-update is off and the app ships and updates the engine. The relay list is signed with the same key. |
| Release signing | Developer ID + notarization in the public repo's release workflow on a GitHub-hosted macOS runner, from repo secrets the owner provides (action item). Until then releases are unsigned and used only from source on our Macs. |

## 3. Identity

- **Computer key**: Ed25519, generated at first run, never leaves the computer. **Member id** = that key.
- **`person_id`** is assigned when a computer is admitted and is **sticky**: the Clerk user id if the computer
  joined with a proof, else its own key. A later proof for a computer whose `person_id` is its own key writes a
  signed `merge` entry (computer key → Clerk id) and from then on `person_id` is the Clerk id. Proof expiry
  changes only the verified badge, never `person_id`, so Allow, labels and limits never flip.
- **Proof** (`lightbulb signin`): compact token `{iss, kid, iat, exp (24 h), sub: person_id, name, computer,
  kind: computer|bridge, workspace?}`, Ed25519-signed by the issuer; verified offline against two compiled-in
  issuer keys (rotation by `kid` overlap). The issuer is a backend route under device-code auth that creates no
  org, no trial and no provisioning. No revocation in v1: a banned person stays "verified" for up to 24 h; trust
  never rests on the badge, only on the roster.
- **Names**: every member has a roster-unique network name (`aden`, `aden-2`), which is what labels render. The
  proof's display name is shown only as a verified check beside it, never used for trust or disambiguation.

## 4. Membership

- **Roster**: a hash-chained signed log replicated to every member: `admit`, `merge`, `remove`, `admin`,
  `transfer`, `leave`, `link` (the team link secret, carried to members; locally it lives in `team.link_secret`), `meta`. On a fork, removal wins. Entries stamped more than
  a day ahead are refused.
- **Who may do what** (owner): any member invites; only the founder and founder-marked admins remove. The
  founder may `transfer`; a founder who leaves without transferring is succeeded by the oldest admin.
- **Admission needs a person's yes.** A `join` with the team link reaches an online admin (or the inviting
  computer), which raises a hold-shaped prompt — "`<name>` wants to join your network" — and admits only on
  Allow. A link minted `invite --open` admits without the prompt (the convenience mode; the CLI says what it
  gives up). The team link expires after 7 days by default (`invite --new` renews it, `--once` mints a 24 h
  single-use ticket) and is replaced on every removal.
- **Ticket**: base64url of `{team_id, link_secret, inviter_key, relay_hint, up to 6 member keys}`.
- **Same-person admission**: a computer presenting a proof for a `person_id` already in the roster is admitted
  by one of that person's own computers **after a local confirmation on that computer** ("Your new Mac
  `<name>` wants to join. Allow?"). The bridge may admit a same-person computer only on the same terms: it
  relays the request to one of the person's computers for the confirmation; it never admits on its own.
- **Member kinds and roles**: `kind: computer | cloud` (cloud = headless: no dialogs, pull or own-channel
  delivery). A cloud member holding a `kind=bridge` proof has the role `bridge`: it can relay hold decisions and
  same-person join requests for its linked `person_id` and nothing else — no invite, no remove, no promote. Its
  removal is by that person (`lightbulb unlink`) or an admin.
- **Networks per computer**: one. A computer is on one network at a time; the bridge joins the network the
  person's computers are on; proofs attach to the person, not the network.
- **A fresh install has a network from the start** (Codex review of #4295): the daemon's first start with no
  team creates one, founded by this computer and named after the OS user, so `invite` works at once. `join`
  replaces that empty solo network with the one joined. `leave` leaves the network (and recreates the solo
  one); stopping or removing the service is `daemon --uninstall`, a different command.

## 5. Addresses, sessions, labels

- **Session id**: `<member_key_short>/<provider>:<conversation>` (keys are stable; names render).
  `member_key_short` is the first 12 characters of the member key's lowercase base32 encoding (60 bits); the
  engine resolves it by exact match, and admission refuses a key whose 12-character prefix equals an existing
  member's, so a prefix is always unambiguous (Codex review of #4295). Cloud sessions
  are `<bridge_member_key_short>/<agent>` (`…/friday`, `…/muse-via-aden`, `…/grok-bot`), so two people's
  Fridays never collide. **Member** addresses (a key) are used only for decisions and streams; **name**
  addresses (`<member>/<name>`) are a person's `listen --as` mailboxes.
- **Everything the bridge exposes is a session of its cloud member**, so `send_message` has exactly one target
  kind, `session_id`, `find_sessions` lists them, and replies go to the same key. The bridge maps a session key
  to a room turn, a hosted mailbox or a webhook internally; the engine never learns about rooms.
- **Labels** are rendered by the receiver from the roster, never carried as free text: `Your <Agent>` (same
  `person_id`), `<name>'s <Agent>` (another person's computer session), `<App> via <name>` (a session of a
  `bridge`-role member whose proof `sub` equals `<name>`'s `person_id`; any other claim renders as the member's
  own name). `<App>` comes from a short allowlist (Claude, ChatGPT, Muse, Grok Bot, Cursor, Codex, Friday and
  the workspace's agent names); anything else renders as `App "<sanitised, ≤32 chars>"`. Unverified names get
  " (not verified)".
- **Frame** (what the agent sees), copied verbatim from the monorepo's `local_sessions.py` and the
  `lightbulb-session-sharing` skill: `[Lightbulb teammate contribution from <label> {"requestId":"…"}]`, a blank
  line, the defanged body. A frame marker or owner line inside a body is neutralised.

## 6. Wire frames (engine ↔ engine, all inside the encrypted QUIC stream)

| Frame | Fields | Notes |
|---|---|---|
| `presence` | member, seq, sessions[], known[], kept[] | pushed on change; every member sees the whole directory (linking a workspace means the workspace sees it too; the link flow says so) |
| `envelope` | id (ulid), kind: `message|input|resume|pickup`, from (session), to (session or name), reply_to, thread (first id), hops, body, files[], sent_at | Plan A uses `message` only; `input|resume|pickup` are Plan D |
| `ack` / `receipt` | id, state, reason | states in §7 |
| `hold_notice` | hold_id, target_session, from (session), from_label_parts, preview?, created_at | computer → its person's bridge member only; `preview` (≤160 chars of the defanged body) only when `config.toml: preview_to_workspace = true` (owner question pending; default false) |
| `decision` | hold_id, holding_member, verdict: `allow|decline`, scope: `once|person|own_cloud`, nonce, decided_at | signed by the sender; see §7 for who may send it. `own_cloud` = "Always allow" for the person's own cloud member (sets `agents_send_without_asking`); it is accepted only when the held message came from the deciding bridge's own sessions (Codex review of #4295, round 3) |
| `join_request` / `join_decision` | ticket_id, candidate key, name, proof?, verdict | admission (§4) |
| `share_grant` | session, viewer member, input: bool, expires_at | person-signed; authorises `stream` for that viewer (§8) |
| `stream` | grant_id, seq, bytes (≤64 KB) | QUIC flow control is the backpressure |

## 7. Delivery, receipts, limits, holds

- **Tiers**: `push` (Claude Code inbox socket, Codex queue; wakes idle), `next_step` (Cursor, Grok: next tool
  call or turn end), `pull` (any MCP client and every cloud session: unread note on every tool result and
  `check_inbox`). The sender is told the tier.
- **Receipt states**, exactly seven, forward-only: `queued` (sender's outbox, recipient unreachable; retried
  24 h) → `stored` (on the recipient's computer) → `delivered` (handed to the agent) → `read` (consumed by the
  agent's next turn); `held` (waiting for the person, at most 7 days) → `stored` on Allow; `refused` (reason:
  `loop`, `policy`, `declined`, `not_member`, `request_id_reused`, `unsupported`); `expired` (24 h with the
  session gone, 7 days held, or 24 h in the outbox).
- **Limits**: 12 messages per 600 s per (sender principal, recipient session), where the principal is the
  sending **session** for cloud members (so one noisy hosted key starves only itself) and the `person_id`
  otherwise → the 13th is `refused: loop`; agent-to-agent chains without a human message cap at 4 hops in the
  network (room turns keep their own cap of 2); unverified senders are limited per roster slot and admission is
  rate-limited (§4), so minting keys buys nothing; body ≤ 64 KB; files ≤ 5 per message, 128 KB each, 256 KB in
  all, saved inert under `inbox/`. Group sends (`*`, `person/*`, `repo:`) only from a person at the CLI; the
  MCP tool and every peer refuse them.
- **Hold policy** (`policy.get/set`): `people: {person_id: accept|hold|refuse}` (Allow writes `accept`; one
  Allow covers all that person's computer sessions), `cloud_of: {person_id: accept|hold}` (Always allow on a
  hold from *that person's* cloud member — their Friday, their Muse — writes `accept`), and
  `agents_send_without_asking: bool` for the person's **own** linked cloud member. Defaults: the person's own
  *computer* sessions `accept`; the person's own *cloud* member follows `agents_send_without_asking` (default
  `false`, so their own Friday is held until they flip it — today's product behaviour); everyone else `hold`.
  Labels follow `person_id` regardless: a person's own Friday renders `Your Friday` on their computers and
  `Friday via <name>` on a teammate's.
- **Answering a hold**: on the person's computer (app, CLI `approve`/`decline`, or the OS dialog) or by a
  `decision` frame from the person's `bridge`-role member (web and phone Allow; owner-approved improvement).
  The holding computer accepts a relayed decision only if all hold: the signer's roster entry carries a
  `kind=bridge` proof whose `sub` is the hold target's `person_id`; scope is `once`, `person`, or `own_cloud`
  (the last only for a hold whose sender is that bridge's own session: it writes
  `agents_send_without_asking = true`; `cloud_of` entries for other people's agents are set only on the
  computer); the nonce is fresh and `hold_id` + `holding_member` match.
- **Every delivered or listed message carries its `request_id` and `thread`** — in the frame header, in
  `check_inbox` results (engine and hosted) and in `inbox` IPC replies — so any caller can answer a specific
  message with `reply_to` (Codex review of #4295, round 3). The held message
  may itself come from that bridge's sessions — a held Friday message allowed from the phone is the main case
  (Codex review of #4295, 2026-10-22). The computer applies it and signs the decision record with its own
  key. First answer wins; a second gets `not_open`. A relayed `person` decision is recorded with
  `set_by: bridge`, the computer shows "Allowed <name> from your phone", and `lightbulb policy` lists and
  revokes bridge-set entries. **Residual trust, stated** (Codex review of #4295, round 4): a compromised
  backend can send a message as the person's own cloud agent and allow it once, allow a held third-party
  message once, or — because phone Allow is a real per-person Allow (owner's product rule) — allow that
  third party's agents until the person revokes it; a phone-held person key (Secure Enclave) that co-signs
  decisions is Plan D hardening, and until then the visible record is the control.
- **Notifier claim**: the app holds `ui.claim` (20 s lease, 7 s heartbeat) while open; the daemon shows the OS
  dialog only when no claim is live. Both resolve the same hold; a duplicate surface in the race is tolerated.

## 8. Streams (Plan B)

`stream.open` to a member is accepted only from the same `person_id`, or from a member named in a
person-signed `share_grant` for that session (the app's Share action mints one; `input: true` is a separate
grant, Plan D). The channel is unicast and end-to-end encrypted. The app owns the live-terminal feature and
maps its frames onto it. Sharing a terminal to the workspace means granting the person's bridge member, which
decrypts for browser and phone viewers — the **fourth** thing the bridge can read, and only by that explicit
grant. "Anyone with the link" viewing is a bridge feature over such a grant.

## 9. IPC and MCP contracts

- IPC: newline-delimited JSON over the socket, `{id, op, …}` → `{id, ok, …}`; a connection that sent
  `subscribe` also receives `{event, …}` frames. snake_case everywhere. Errors are `{ok:false, error:"<code>"}`
  with a closed code list per op.
- Ops by plan: **A** `ping`, `status`, `subscribe`, `register` (hook and MCP paths only, caller-identified; the
  app never registers), `sessions`, `end`, `send`, `receipt`, `inbox`, `hook`, `tool`, `shutdown`.
  **B** `identity.*`, `team.*` (incl. `join_requests`, `admit`), `directory`, `holds`, `hold.get`,
  `hold.answer`, `policy.get/set`, `ui.claim`, `share.grant`, `stream.*`, `reload`. **C** adds none; the bridge
  uses the engine as a crate.
- MCP (engine and hosted, same names and argument names; each side refuses what it does not serve with
  `unsupported`): `find_sessions(query, limit=20)`, `send_message(request_id, text, session_id, reply_to?,
  files?, notify?)` — the hosted MCP additionally accepts `room_id | person_id | agent_id` for workspace rooms
  and refuses `files`/`notify`; `message_receipt(request_id)`; `check_inbox(wait_secs ≤ 600)`. `read_messages`,
  `find_rooms` and the task tools stay hosted-only.
- **Threads and hops** (Codex review of #4295, rounds 2 and 4): every delivered frame shows its `requestId`.
  `reply_to` names the `requestId` the agent is answering; the daemon sets the envelope's `thread` to that
  message's thread and `hops` to its hops + 1. Without `reply_to`, the **trigger** is the newest message
  delivered to the sender in the last 600 s from any session: `hops` = trigger.hops + 1 (so forwarding work to
  a third session keeps the chain count and the 4-hop cap holds), and `thread` = the trigger's thread when
  the target is the trigger's sender, else a new thread. With no trigger, hops is 0 and the thread is new.
  The bridge routes replies by `thread`; the frame header tells the agent to pass `reply_to` when it answers
  a specific message.
- Session record (both MCPs): `{session_id, person, person_verified, computer, agent, cwd, repo, branch,
  status: busy|idle|gone, doing, tier, can_send, needs_owner_ok}`.
- Plan A's schema is the final shape of its tables: `messages` carries `kind`, `thread`, `hops`, `origin`;
  `user_version` starts at 1 and Plan B only adds tables.

## 10. The bridge

- A Rust service in the private monorepo (`apps/bridge/`), the engine as a crate, one member identity per
  (person, workspace) link, keys in Secrets Manager. One ECS task in v1 (ponytail: shard by person hash when
  needed). It dials out through relays, so it needs no inbound port.
- The backend reaches it over internal HTTP (`/bridge/send`, `/bridge/decide`, `/bridge/directory`); inbound
  mail for its sessions is posted to the backend (`/internal/bridge/inbound`), correlated by `thread`, which
  routes to a room turn, a hosted mailbox (kept 7 days, or 24 h after read) or a webhook.
- What the bridge can read: mail addressed to its sessions, hold notices for its person, the directory, and
  streams explicitly granted to it. Nothing else, enforced by the encryption. It logs ids, sizes and states,
  never bodies.
- Hosted MCP access needs a Lightbulb workspace; a free trial counts (every org gets one). The copy says "a
  Lightbulb workspace", not "paid".

## 11. Cutover and deletion

- **Plan A is engine-only**: no monorepo change (its former Task 12 is dropped). Plans B and C land in the
  public repo and then the monorepo in one cutover release: the app attaches to the engine, the bridge comes
  up, and in the same train these are deleted — desktop `transport.rs`, `claude.rs`, `codex.rs`, `hooked.rs`,
  `enrollment.rs`, `lifecycle.rs`, `native_mcp.rs`, `native_tools.rs`, `plugin.rs`, `approval.rs`,
  `held_notice.rs`, `presence.rs`, `remote_input.rs`, `snapshot.rs`; backend `local_sessions.py`,
  `local_session_relay.py`, `local_session_grants.py`, `local_sessions_repo.py`, the `/local-sessions/device/*`
  routes and the `lightbulb-local-sessions` websocket client. Kept and rewired: `hosted_mcp`, `agent_keys`,
  `webhook_agents`, `device_login` (becomes the proof flow), the holder, `screen.rs` (frames onto `stream`),
  `resume.rs` (person-triggered only), `notify.rs`, the mod socket.
- **Hub-era features at cutover**: phone-answered Claude permission prompts ride the `decision` path and stay
  (Plan C). Teammate typing into your terminal, teammate-requested resume and session pick-up become envelope
  kinds `input`, `resume`, `pickup` under the same hold rules in Plan D unless Plan C has room.
- There is no intermediate build to test: the parity checklist runs on the cutover build.

## 12. Open with the owner (sent 2026-10-22)

1. Hosting spend for the default relays (one small VPS to start; more regions later) before Plan B.
2. Phone previews of held messages (first 160 characters, opt-in at link time) or "Needs your Mac" only.
3. Developer ID certificate and notary credentials as public-repo secrets for signed engine releases.

FYI rulings the owner may overturn: iroh as the transport library (our own relays only); admission needs an
admin's Allow unless the link was minted `--open`; the three hub-era collaboration features deferred to
Plan D; "needs a workspace (trial counts)" rather than "paid" for the hosted MCP; Muse Code support deferred
past parity.
