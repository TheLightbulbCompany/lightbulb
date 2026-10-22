# lightbulb-network, Plan B: the network

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two computers on two accounts form one network: a signed roster every member checks, invite tickets, admission on a person's Allow, a live directory, envelopes with an outbox and receipts, holds with an OS dialog, and streams. Acceptance: the design's checklist rows 2, 3, 4, 9, 10 between two Macs (Task 15).

**Architecture:** Builds on Plan A (`lightbulb-network` at `462b467`) without rewriting it. The daemon gains a QUIC endpoint (`iroh` as a library) keyed by the existing `identity.key`, so the member key IS the transport key and every connection is authenticated to a member. Membership is a hash-linked DAG of signed entries with one root, replayed deterministically on every computer. A message to a session whose `member_key_short` is not ours goes to the outbox (`queued`), crosses as an `envelope`, and on the recipient enters Plan A's mailbox (`stored`) or the new hold table (`held`). Team state becomes mutable (`Mutex<TeamState>`) because `join` and `leave` replace it at runtime. New IPC ops live in `src/daemon/net_ops.rs`, reached from `ipc::handle`'s fallback arm. LAN and direct paths land first (Tasks 1–11); relay fallback (12), sign-in proofs (13) and relayed decisions (14) can each wait without breaking the rest.

**Tech Stack:** Plan A's (Rust 2021, `tokio`, `rusqlite`, `rmcp`, `serde_json`, `clap`, `ed25519-dalek`, `data-encoding`, `sha2`, `toml_edit`, `ulid`) plus `iroh` (exact pin), `ureq` (Task 13 only, blocking HTTPS for the device flow), `minisign-verify` (Task 12 only).

**Spec:** `docs/superpowers/specs/2026-10-22-lightbulb-network-architecture.md` (binding, §3–§9), `…-engine-spec.md`, `…-p2p-design.md`.

## Global Constraints

- **Clean-room.** Do not open anything named reason-network or reasonmachines, nor the monorepo's `apps/desktop/src-tauri/src/local_sessions/hooked.rs`. Every design here comes from our three specs.
- The engine knows nothing of workspaces, plans, rooms or billing. Its one Lightbulb-specific default is `config.toml: issuer = "https://thelightbulb.company/network"`.
- Paths: state `~/.lightbulb/network/` (0700, override `LIGHTBULB_NETWORK_HOME`); new file `config.toml` (0600). `state.db` goes from `user_version` 1 to 2 by **adding tables only**; a db newer than the binary refuses to start with `State is from a newer version (v<N>). Install <version> or later.` The keychain move of `identity.key` is Plan D and must be keyed by state dir when it lands.
- Member key = Ed25519 public key from `identity.key`, lowercase base32 without padding; `member_key_short` = its first 12 characters. Session id `<member_key_short>/<provider>:<conversation>`; name address `<member_key_short>/<name>`.
- Roster entry kinds: `admit`, `merge`, `remove`, `admin`, `transfer`, `leave`, `link`, `meta` (architecture §4) plus `proof` (renews a member's proof; coordinator ruling). Roles `member|admin|founder`; member kinds `computer|cloud`; the `bridge` role = a cloud member whose roster proof is `kind=bridge`. `team_id` = the genesis entry's hash. On a fork, removal wins. Entries stamped > 86 400 s ahead are refused. `person_id` is sticky.
- **Signing.** Every signed thing (roster entries, tickets, `decision`, `share_grant`, `join_request`, `join_decision`) carries `sig` = Ed25519, base64url, over `identity::canonical(object without sig)`: JSON with keys sorted recursively and no whitespace (sorted explicitly, never relying on serde_json's map order, since a dependency may enable `preserve_order`).
- Ticket: base64url (no padding) of signed JSON `{v:1, ticket_id, team_id, secret, mode: "ask"|"open", inviter, relay_hint, members: [≤6 keys], expires_at, sig}`. Reusable link 7 days; `--once` 24 h, single use, stored hashed, deleted on use. Formats: `Join the Lightbulb network: https://thelightbulb.company/j/#<ticket>` and `lightbulb://join/<ticket>`.
- Wire: ALPN `lightbulb-network/1` (members) and `lightbulb-network/join/1` (candidates); one u32-BE-length-prefixed JSON frame (≤ 1 MiB, tag field `t`) per message. The `Frame` enum is defined once, in Task 4, with every field.
- Receipt states, exactly seven, forward-only: `queued`, `stored`, `delivered`, `read`, `held`, `refused` (reasons `loop`, `policy`, `declined`, `not_member`, `request_id_reused`, `unsupported`), `expired`. Outbox retries 24 h (backoff capped at 60 s), then `expired` reason `unreachable`. Recipient mailbox 24 h (Plan A `HOLD_S`). A hold lasts at most 7 days (`HELD_S`). `seen_ids` kept 7 days.
- Limits: 12 per 600 s per (sender principal, recipient session), principal = `person_id` for computer members and the sending session for cloud members; agent chains cap at 4 hops (`hops >= 4` refused `loop`, sender sees `paused after 4 agent replies; a person must speak`); body ≤ 64 KB; ≤ 100 sessions, `known` and `kept` names each per presence; peers never send groups.
- Policy: `people {person_id: accept|hold|refuse}`, `cloud_of {person_id: accept|hold}`, `agents_send_without_asking: bool` (default false). Defaults: own `person_id`'s computer sessions accept; own cloud member follows the switch; everyone else hold.
- **Decision scope, one enum everywhere:** `once | person | own_cloud | cloud_of`. `person` writes `people[pid]=accept`; `cloud_of` (Always allow on ANOTHER person's cloud member) writes `cloud_of[pid]=accept`; `own_cloud` (Always allow on your OWN cloud member) writes `agents_send_without_asking = true`. Local answers may use all four; a relayed `decision` only `once | person | own_cloud`. First answer wins; a second gets `not_open`.
- **Person-only ops** (`hold.answer`, `hold.get`, `team.admit`, `policy.set`, `team.invite` with `open`, `share.grant`, `team.unlink`) are refused `bad_request` (reason `not_a_person`) by the DAEMON when the socket peer's pid (`os::peer_pid`, macOS `LOCAL_PEERPID`) has an agent ancestor by Plan A's `caller` classification. The CLI's own check stays as the friendly message.
- `ui.claim`: 20 s lease, 7 s heartbeat; the daemon shows an OS dialog only when no claim is live; one dialog on screen at a time, the rest queued.
- IPC ops added (architecture §9 B): `identity.get`, `identity.proof`, `team.get`, `team.invite`, `team.join`, `team.join_requests`, `team.admit`, `team.remove`, `team.admin`, `team.leave`, `team.unlink`, `directory`, `holds`, `hold.get`, `hold.answer`, `policy.get`, `policy.set`, `ui.claim`, `share.grant`, `stream.open`, `stream.send`, `stream.close`, `reload`. New error codes: `not_admin`, `not_founder`, `not_open`, `name_taken`, `key_collision`, `bad_ticket`, `expired_ticket`, `not_member`, `founder_must_transfer`, `no_grant`, `unreachable`.
- Debug-build-only knobs (compiled out of release, like Plan A's `CALLER_ENV`): `LIGHTBULB_NETWORK_PEERS=<key>@<ip:port>,…`, `LIGHTBULB_NETWORK_DIALOG=<button>`, `LIGHTBULB_NETWORK_DEBUG_DROP_ACKS=1`, `LIGHTBULB_NETWORK_DEBUG_PROTOCOL=<n>`, and a test issuer key under `#[cfg(debug_assertions)]`.
- Logs: ids, sizes and states only; never a body, key or teammate path. Ponytail at every step; `/simplify` after every task before review.

## Review Focus

1. **Fork convergence, deep forks included.** An entry whose signer's `remove` is concurrent with it must be dropped identically everywhere, however deep the fork and whatever the arrival order (Task 2: `deep_fork_removal_wins`, `replay_is_order_independent`).
2. **Every frame is checked against the connection's peer.** A removed member's connection closes at the removal; `presence` naming another prefix, an `envelope` `from` another prefix, or an `ack` from a member that is not the outbox row's `to_member` is dropped (Task 4, Task 7, Task 8).
3. **Admission without a person's yes.** An `ask` ticket re-signed `open`, a used `--once` ticket, a `candidate` that is not the peer key, a 12-character prefix or name collision must never admit (Task 2: `prefix_collision_is_refused`; Task 6).
4. **The outbox never loses or doubles.** Dropped acks resend; `seen_ids` acks the duplicate without a second delivery; `stored` appears only after the recipient's ack (Task 8: `outbox_survives_restart_and_dedupes`).
5. **A hold answered from the wrong place.** An agent shell cannot `approve`; the dialog text never enters the AppleScript; a relayed `cloud_of` or another person's bridge is refused (Task 5, Task 9: `approve_from_agent_shell_is_refused`, Task 14).

---

### Task 1: State v2, `config.toml`, mutable team state, identity ops, person-only guard

**Files:** Create `src/identity.rs`, `src/config.rs`, `src/daemon/net_ops.rs`. Modify `src/store.rs`, `src/daemon/mod.rs` (move `identity()` into `identity.rs`; `Daemon.member_key_short/team_id/team_name` become `Daemon.team: Mutex<TeamState>`, callers updated), `src/daemon/ipc.rs` (fallback arm → `net_ops::handle`; each connection records its peer pid), `src/lib.rs`. Test: `src/store.rs`, `src/config.rs`, `tests/identity_ipc.rs`.

**Interfaces:**
- Produces: `identity::Identity { signing: SigningKey, key: String, short: String }`, `Identity::load_or_create(dir) -> Result<Identity>` (Plan A's atomic write kept), `Identity::sign(&self, v: &Value) -> String`, `identity::verify(key: &str, v: &Value, sig: &str) -> bool`, `identity::canonical(v: &Value) -> Vec<u8>`.
- Produces: `config::Config { computer: String, issuer: String, preview_to_workspace: bool, share_paths: bool, lan: bool, relay: Option<String>, folders_off: Vec<PathBuf> }`, `Config::load(dir)` (missing file = defaults; `computer` = `os::hostname()`), `Config::set(dir, key, value)` (toml_edit).
- Produces: `daemon::TeamState { team_id: String, team_name: String, heads: Vec<String>, config: Config }`; `Daemon.identity: Identity` (`member_key_short` is `identity.short`).
- Store: `Store::open` reads `PRAGMA user_version` FIRST: > 2 → refuse; 0 → Plan A `SCHEMA` (its `PRAGMA user_version = 1` moved out of the batch and run only here); then migration 2, all `CREATE TABLE IF NOT EXISTS` (re-runnable), then `user_version = 2`. Tables: `roster(hash PK, entry, received_at)`, `team_link(ticket_id PK, secret, secret_hash, mode, once, expires_at, used_by)`, `join_requests(id INTEGER PK, ticket_id, inviter, candidate, name, computer, kind, proof, via, created_at, state)`, `peers_sessions(member PK, seq, presence, seen_at)`, `outbox(message_id PK, to_member, frame, attempts, next_at, created_at)`, `receipt_out(member, envelope_id, state, reason, PK(member, envelope_id))`, `seen_ids(sender, envelope_id, state, seen_at, PK(sender, envelope_id))`, `holds(id INTEGER PK, message_id, target_session, from_session, from_member, created_at, state)`, `policy(scope, person_id, value, set_by, updated_at, PK(scope, person_id))`, `approvals(hold_id, verdict, scope, by, nonce UNIQUE, decided_at, record)`, `grants(grant_id PK, grant, expires_at)`. The `team_link` row is what the architecture calls `team.link_secret`.
- Produces: `net_ops::person_only(peer_pid: u32) -> bool` (false when an ancestor classifies as an agent; in debug, `CALLER_ENV` names the ancestor), checked by every person-only op.
- Produces op `identity.get {}` → `{member_key, member_key_short, name, computer, person_id, verified}`.

- [ ] **Step 1: Failing tests**
```rust
#[test] fn v1_db_migrates_to_v2_adding_tables_only() { /* Plan A v1 db with a message row; open twice; user_version == 2; row unchanged; "holds" exists */ }
#[test] fn newer_db_refuses_to_open() { /* set user_version = 9; Store::open -> Err containing "State is from a newer version (v9)"; user_version still 9 */ }
#[test] fn canonical_sorts_keys_and_drops_whitespace() { assert_eq!(canonical(&json!({"b":1,"a":{"d":2,"c":3}})), br#"{"a":{"c":3,"d":2},"b":1}"#); }
#[test] fn config_defaults_and_set_round_trip() { assert_eq!(Config::load(d).unwrap().issuer, "https://thelightbulb.company/network"); }
#[tokio::test] async fn identity_get_matches_status() { /* identity.get.member_key_short == status.member_key_short */ }
```
- [ ] **Step 2:** `cargo test` — FAIL. **Step 3:** Implement; migrations are an ordered `&[&str]`, forward only. **Step 4:** PASS. **Step 5:** Commit `feat: state v2, config.toml, mutable team state, identity ops`.

### Task 2: The roster log

**Files:** Create `src/roster.rs`. Test: `src/roster.rs`.

**Interfaces:**
- Produces: `roster::Entry { v: u32, parents: Vec<String>, kind: Kind, body: Value, signer: String, at: i64, sig: String }`; `Entry::hash()` = hex sha256 of `canonical(entry without sig)`; `Entry::signed(id: &Identity, parents, kind, body, at) -> Entry`.
- Bodies: `admit {member, name, computer, person_id, kind, role, ticket_id, proof?}` (genesis: `parents: []`, `role: "founder"`, plus `team_name`); `merge {member, person_id, proof}`; `proof {member, proof}`; `remove {member}`; `admin {member}`; `transfer {member}`; `leave {member}`; `link {ticket_id, secret, mode, once, expires_at}` (`once: true` → `secret` is the ticket secret's hex sha256); `meta {team_name?, relay?}`.
- Who signs what (checked at the entry's point in replay):

| Kind | Valid signer |
|---|---|
| `admit` | founder or admin, or the active **computer** member who signed the live `link` entry named by the body's `ticket_id` — the inviter, so "any member invites" holds with no admin online (Codex, #4297); a cloud/bridge member never admits through a link. A `link` with `once: true` admits exactly once: the first `admit` naming it in replay order is applied and ends the link; every later or concurrent `admit` naming it is dropped — consumption is replicated by the roster, never by a local table (Codex round 2) (genesis: the founder itself; Task 13: a computer of the same `person_id`, body carrying a proof whose `sub` is that `person_id`) |
| `merge`, `proof` | the member itself, with a proof valid for it (Task 13) |
| `remove` | founder or admin; also a member removing its own person's bridge member (`unlink`) |
| `admin`, `transfer` | founder (architecture §4: "only the founder and founder-marked admins remove"; "the founder may transfer") |
| `link` | any active **computer** member (the inviter; a cloud/bridge member's `link` is dropped — a bridge only relays decisions and same-person joins); a `link` is live only while it descends from EVERY applied `remove` (a removal ends every link minted before or concurrently with it, so a ticket the removed member already holds is dead even at an admin who never saw the link — Codex round 3), until its `expires_at`, until its signer applies a newer non-`once` `link` of the same `mode` (so `invite --new` retires the old `ask` link while an `open` link coexists with it; `once` links coexist too), and, when `once`, until its first applied `admit`; an `admit` via a link stands only if it was already an ancestor of every removal that ends the link |
| `meta` | founder or admin |
| `leave` | the leaving member |

- Produces: `roster::Roster { team_id, team_name, members: BTreeMap<String, Member>, heads: Vec<String>, links: BTreeMap<String, Link>, relay: Option<String> }` (`links` = the live `link` entries by `ticket_id`, each with its signer), `Member { key, short, name, computer, person_id, role: Role, kind: MemberKind, admitted_at, proof: Option<String> }`; `roster::replay(entries: &[Entry]) -> Roster`; `roster::accept(store, entry, now) -> Result<bool>` (verifies sig; refuses `at > now + 86_400`, unknown parents, and any second parentless entry); `Roster::by_short(short)`; `Roster::ancestors(hash) -> HashSet<String>`.
- Replay rule: Kahn topological order (ready `remove` entries first, then lowest hash). An entry is applied only if its signer is active and authorised there AND no `remove` of its signer is concurrent with it (neither its ancestor nor its descendant); otherwise it is dropped, which drops everything only it admitted. A `remove` is judged on its own causal past: a removal of ITS signer that is merely concurrent does not stop it, so three admins removing each other concurrently all go. Removal-wins needs more than one pass (a removal deeper in its branch replays after the concurrent entry it must drop): replay iterates with `known` = the monotone UNION of removals applied so far, a known removal bypasses only the concurrent-removal check and takes effect in a pass only while its signer's admission is applied in that pass, and the loop ends when `known` stops growing (cap #removes+1, deterministic either way). The binding guarantee is determinism over the entry SET; insensitivity to unrelated entries is required for every tested shape. An `admit` with no `proof` must carry `person_id == member` (a proof, Task 13, is the only way to claim another id); an `admit` whose name or 12-char prefix equals an active member's is dropped; a `link` whose `ticket_id` was ever seen before, or whose `ticket_id` does not start with its signer's 12-character `short` followed by `.`, is dropped (so a `ticket_id` names one link entry and another member cannot front-run it; Task 3 mints `ticket_id = <short>.<ulid>`). Founder `leave` without `transfer` promotes the oldest admin. Exactly one root; every entry descends from it.

- [ ] **Step 1: Failing tests**
```rust
#[test] fn genesis_makes_a_founder_and_names_the_team() { let r = replay(&[g.clone()]); assert_eq!(r.team_id, g.hash()); assert_eq!(r.members[&a].role, Role::Founder); }
#[test] fn non_admin_admit_and_remove_are_dropped() {}
#[test] fn fork_removal_wins_and_drops_admissions_by_removed_signer() { /* same parent: X admits C | founder removes X -> X, C absent */ }
#[test] fn deep_fork_removal_wins() { /* fork at F: branch 1 = X admits C, then X admits D (two entries below F); branch 2 = founder removes X -> X, C, D absent */ }
#[test] fn replay_is_order_independent() { /* every arrival permutation of both forks -> equal Roster */ }
#[test] fn prefix_collision_is_refused() { /* crafted key pair sharing B's 12-char prefix -> its admit dropped */ }
#[test] fn name_collision_is_refused() {}
#[test] fn second_root_and_far_future_are_refused() { assert!(accept(&s, other_genesis, now).is_err()); assert!(accept(&s, e_at(now + 86_401), now).is_err()); }
#[test] fn founder_leave_promotes_oldest_admin() {}
#[test] fn person_id_is_sticky_and_merge_rewrites_it_once() {}
#[test] fn inviter_may_admit_through_its_own_link_only() { /* member X writes link L; X's admit {ticket_id: L} applied; X's admit {ticket_id: founder's link} dropped; after founder removes Y, L (an ancestor of the remove) is no longer live and a later admit via L is dropped */ }
#[test] fn once_link_admits_exactly_one_even_on_a_fork() { /* once-link L; fork: X admits C via L | founder admits D via L -> exactly one of C, D present, the same one in every arrival order; L absent from links */ }
#[test] fn bridge_member_cannot_link_or_admit() { /* a kind=cloud member's link entry dropped; its admit via a computer member's link dropped */ }
#[test] fn removal_cycle_converges_and_is_stable() { /* admins A→B, B→C, C→A concurrently: all three gone; identical roster with 0..3 extra meta entries */ }
#[test] fn stale_known_removal_stops_when_its_signers_admission_drops() { /* X (plain member, link t) admits Y; Y's remove of M concurrent with founder's remove of X: Y, X absent; M present whether the founder's remove is a direct child of the fork or sits behind two later-sorting meta entries */ }
#[test] fn admit_cannot_claim_another_persons_id() { /* admit without proof with person_id = founder's -> dropped */ }
#[test] fn duplicate_ticket_id_link_is_dropped() {}
#[test] fn link_concurrent_with_a_removal_is_ended() { /* F mints L; admin A removes X without L in its ancestors: L absent from links; an admit via L not ancestral to the remove dropped; one ancestral to it stands */ }
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement (pure; no network). **Step 4:** PASS. **Step 5:** Commit `feat: hash-linked signed roster with removal-wins replay`.

### Task 3: Team ops and tickets (local only)

**Files:** Create `src/ticket.rs`. Modify `src/daemon/net_ops.rs`, `src/daemon/mod.rs` (`run()` writes a genesis only when the `roster` table is EMPTY; `TeamState` is built from the replayed roster and the Plan A `team` row rewritten from it), `src/cli.rs`, `src/main.rs`. Test: `src/ticket.rs`, `tests/team_cli.rs`.

**Interfaces:**
- Produces: `ticket::Ticket` (Global Constraints shape), `ticket::mint(id, roster, mode, once, now) -> (Ticket, Option<String>)` (`ticket_id = <id.short>.<ulid>` — replay drops a `link` whose `ticket_id` is not prefixed by its signer's short, Task 2; members = the inviter first, then founder and admins, ≤6), `ticket::encode`, `ticket::decode(s) -> Result<Ticket>` (bare ticket, `/j/#…` link, full join line, or `lightbulb://join/…`), `ticket::verify(&Ticket, roster) -> Result<()>` (sig by `inviter`; `team_id`; the link `roster.links[ticket_id]` exists and its `signer == inviter` and `mode == ticket.mode` — a member who learned another member's link secret cannot re-sign it as its own or as `open` (Codex round 2)), `ticket::line(&Ticket) -> String`.
- Ops: `team.get {}` → `{team_id, name, members: [Member], heads}`; `team.invite {mode: "reusable"|"new"|"once"|"open"}` → `{ticket, line, app_link, expires_at}`: `reusable` reprints this member's live link, minting one (`new`) when it has none; `new` and `once` are open to any member and each write a `link` entry this member signs (`once`: `secret` = the sha256, `once: true`), because the inviter may sign the `admit` it leads to (Task 2); `open` needs founder or admin (`not_admin`); `team.admin {member, founder}` (`not_founder`); `team.remove {member}` → `{line}` (`remove`, which ends every live `link`, then a new `link`; `not_admin`); `team.leave {}` writes `leave`, deletes roster and `team_link`, writes a fresh genesis (the solo network again); `founder_must_transfer` when the founder has no admin. Telling peers about the `leave` lands in Task 8.
- CLI: `invite [--new|--once|--open]`, `team`, `admin <name> [--founder]`, `remove <name>`, `leave` (asks `Leave "<team>"? Your computer stops connecting. Mail already stored here stays. [y/N]`, ends `Left "<team>". This computer is on its own network again.`; the service keeps running, architecture §4, which overrides the design's row 10 and engine flow 7). Other texts exactly as engine spec flows 3 and 7.

- [ ] **Step 1: Failing tests**
```rust
#[test] fn ticket_round_trips_through_every_link_form() { for s in [bare, line, web, app] { assert_eq!(decode(s).unwrap(), t); } }
#[test] fn ticket_mode_is_covered_by_the_signature() { /* mode edited to "open" -> verify Err */ }
#[test] fn ticket_must_match_its_links_issuer_and_mode() { /* member Y validly signs a ticket reusing X's ticket_id+secret with mode open -> verify Err; same with inviter: Y -> Err */ }
#[test] fn invite_reprints_the_same_link_until_new() { /* `invite --new` prints "Old link stopped working. New link: " */ }
#[test] fn non_admin_remove_prints_exact_text() { /* "Only the founder and admins can remove computers. Ask <founder>." */ }
#[test] fn restart_keeps_the_team_and_writes_no_second_genesis() { /* start, stop, start: team_id unchanged; roster has 1 entry */ }
#[test] fn leave_locally_recreates_a_solo_network() {}
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement; `--once` secrets stored as sha256, the reusable secret in plaintext (architecture §2). **Step 4:** PASS. **Step 5:** Commit `feat: team ops, tickets, invite/admin/remove/leave`.

### Task 4: Transport, the `Frame` enum, the gate, roster sync

**Files:** Create `src/net/mod.rs`, `src/net/frames.rs`. Modify `Cargo.toml` (`iroh = "=<newest release at task start>"`, version recorded in the commit), `src/daemon/mod.rs` (start `Net` beside the socket), `src/daemon/ipc.rs` (`status` gains `{peers: {online, total}, relay: "off"}`). Test: `tests/net_two.rs`, `tests/support/mod.rs` (spawns daemons with temp `LIGHTBULB_NETWORK_HOME`s and `LIGHTBULB_NETWORK_PEERS`).

**Interfaces:**
- Produces `frames::Frame`, serde-tagged `t`, every field later tasks use (✍ = carries `sig`): `Hello {protocol, heads}`; `Roster {entries, more}` (✍ per entry); `Presence {member, seq, sessions: [Remote], known: [String], kept: [String]}`; `Envelope {id, request_id, kind, from, to, reply_to, thread, hops, body, files, sent_at}`; `Ack {id, state, reason}`; `Receipt {id, state, reason}`; `HoldNotice {hold_id, target_session, from, from_label_parts, preview, created_at}`; `Decision {hold_id, holding_member, verdict, scope, nonce, decided_at, sig}` ✍; `JoinRequest {ticket, secret_proof, candidate, name, computer, kind, proof, sig}` ✍; `JoinDecision {ticket_id, candidate, verdict, reason, sig}` ✍; `ShareGrant {grant_id, session, viewer, input, expires_at, granter, sig}` ✍; `Stream {grant_id, session, seq, bytes}` (bytes base64, ≤ 65 536 decoded). `Envelope.request_id` is the sender's id the receiving agent sees (§7). Transient frames carry no `sig`: the QUIC handshake authenticates the peer (ponytail: sign envelopes if they are ever forwarded).
- Produces: `frames::write(send, &Frame)`, `frames::read(recv) -> Result<Frame>` (≤ 1 MiB); `net::ALPN`, `net::JOIN_ALPN`, `net::PROTOCOL: u32 = 1` (debug: `LIGHTBULB_NETWORK_DEBUG_PROTOCOL` overrides); `net::Net::start(daemon) -> Result<Net>` (endpoint secret = `identity.key` bytes, relays disabled, mDNS when `config.lan`); `Net::send(member, Frame)`; `Net::online() -> Vec<String>`. One connection per member pair (the lower key's dial wins); dial every active member on start, on roster change and every 30 s.
- Gate: accept on `ALPN` only from an active member; after every applied roster entry, close connections to now-inactive keys. `frames::from_peer_ok(&Frame, peer: &Member, daemon) -> bool` is the one per-frame check (rules added by Tasks 7, 8, 14); a frame failing it is dropped and counted in the log. A `hello` with another `protocol` shows in `status` as `<computer> needs an update (protocol <theirs>, yours <ours>)` and no traffic follows.
- Sync: `hello.heads`; each side sends in `roster` the entries that are not ancestors of the peer's heads (the full log when it knows none of them), **in its own replay order (parents before children) across page boundaries**, paged by ENCODED size — a `roster` frame closes when adding the next entry would take it past 512 KiB (half the 1 MiB frame cap; a `proof` entry can carry a 4 KiB token), with 500 entries as a secondary ceiling — `more: true` until the last (Codex round 4). The receiver applies in order; an entry whose parents are still unknown waits in a per-connection pending set, retried after every frame and discarded when the connection closes; heads advance only past applied entries, so a dropped page is re-requested on the next `hello` (Codex, #4297).

- [ ] **Step 1: Failing tests**
```rust
#[tokio::test] async fn two_members_connect_and_converge_rosters() { /* A writes a meta entry; B's heads equal A's within 2 s */ }
#[tokio::test] async fn sync_sends_only_the_delta_in_pages() { /* B lags 1 200 entries -> 3 roster frames, last more=false, every entry's parents earlier in the stream; an up-to-date peer gets 0 entries */ }
#[tokio::test] async fn child_before_parent_in_one_frame_still_converges() { /* a frame hand-built with a child first: child pends, parent applies, child applies; heads equal */ }
#[tokio::test] async fn roster_pages_split_by_bytes_not_only_count() { /* 300 entries with 4 KiB bodies -> more than one frame, every frame ≤ 512 KiB encoded, all applied */ }
#[tokio::test] async fn non_member_is_refused_on_member_alpn() {}
#[tokio::test] async fn removed_member_connection_is_closed_on_removal() { /* closes within 2 s; redial refused */ }
#[tokio::test] async fn older_protocol_is_reported_not_served() { /* B started with DEBUG_PROTOCOL=0 */ }
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: quic transport over iroh, frames, membership gate, delta roster sync`.

### Task 5: OS dialog and `ui.claim`

**Files:** Create `src/dialog.rs`. Modify `src/daemon/net_ops.rs`. Test: `src/dialog.rs`, `tests/ui_claim.rs`.

**Interfaces:**
- Produces: `dialog::args(title: &str, text: &str, buttons: &[&str]) -> Vec<String>` = `-e 'on run argv' -e 'display dialog (item 2 of argv) with title (item 1 of argv) buttons {…} giving up after 600' -e 'end run' -- <title> <text>` (button names are fixed strings, never user text); `dialog::Queue::show(id, args)` runs one `osascript` at a time and queues the rest; `Queue::withdraw(id)` kills or dequeues it; answers arrive as `(id, Option<button>)`. Debug: `LIGHTBULB_NETWORK_DIALOG=<button>` answers without a window. If `osascript` fails (no GUI, SSH) the item waits for the CLI.
- Produces op `ui.claim {}` → `{lease_s: 20}`; `Daemon::claim_live(now) -> bool`; no dialog is raised while a claim is live.

- [ ] **Step 1: Failing tests**
```rust
#[test] fn dialog_text_is_an_argument() { let t = "\" & do shell script \"x\" & \""; let a = args("T", t, &["Allow"]); let i = a.iter().position(|s| s == "--").unwrap(); assert!(a[..i].iter().all(|s| !s.contains("do shell"))); assert!(a[i + 1..].contains(&t.to_string())); }
#[tokio::test] async fn one_dialog_at_a_time() { /* show 2; the second starts only after the first answers */ }
#[tokio::test] async fn claim_suppresses_dialog_for_20_seconds() { /* claim_live(t+19) true; claim_live(t+21) false */ }
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: os dialog queue and ui.claim`.

### Task 6: Admission: `join`, `join_request`, `join_decision`

**Files:** Create `src/admission.rs`. Modify `src/net/mod.rs` (`JOIN_ALPN` handler), `src/daemon/net_ops.rs`, `src/cli.rs`, `src/main.rs`. Test: `tests/join.rs`.

**Interfaces:**
- Ops: `team.join {ticket, name, kind: "computer"}` → `{state: "admitted"|"waiting"|"refused", reason?, team, online}`: dials ticket members in order on `JOIN_ALPN`, sends a signed `JoinRequest` (`secret_proof` = hex sha256(secret ‖ candidate)), waits ≤ 600 s for `JoinDecision` then `Roster`, verifies every entry itself, replaces its roster and `TeamState`; `team.join_requests {}` → `{requests: [{id: "J<n>", candidate, name, computer, created_at}]}`; `team.admit {id, verdict}` (person-only; `not_open` on a second answer).
- Admitting side, in order: `candidate` == the connection's peer key (else drop); request `sig`; `ticket::verify` (sig by `inviter`, `team_id`, link exists with `signer == inviter` and `mode == ticket.mode`), not expired on this clock (`expired_ticket`); secret matches that live `link` (plaintext, or its sha256 when `once`) (`bad_ticket`) — the roster is the truth for `once` consumption (Task 2 applies only the first `admit`; a losing concurrent admission is dropped on replay and that candidate must join again), the local `team_link.used_by` is only a hint to refuse early; `kind: "cloud"` → refused `unsupported` until Task 13 (needs a `kind=bridge` proof); name and prefix free (`name_taken`, `key_collision`, the same checks replay enforces); this computer is a computer member that is founder, admin, or the signer of that `link` entry (`not_admin`, candidate tries the next member; a bridge never admits); at most 20 open requests per team, 5 per inviter, 1 per candidate — counting only `open` rows younger than 600 s: a request the candidate stopped waiting for (600 s, or its connection closed) is marked `expired` and frees the limits (Codex round 4). `mode: open` admits at once; `ask` queues `dialog::Queue::show` with `"<name> wants to join your network"`, buttons `Decline`, `Allow`. Admit writes `admit {person_id: candidate, kind, role: "member"}`; a used `--once` hash is deleted.
- CLI: `join '<link>' [--name <n>]` (default `$USER`) prints `Waiting for an admin to allow this...` then `Connected as <name> (<computer>). Team "<team>": <n> computers online.`; name taken → `The name "<n>" is already used in this network. Try: lightbulb-network join '<link>' --name <n>-2`; nobody reachable → engine spec flow 2.6 text. `approve J<n>` / `decline J<n>`.

- [ ] **Step 1: Failing tests**
```rust
#[tokio::test] async fn ask_ticket_admits_only_after_allow() { /* J1 listed; B waiting; admit J1 allow -> both list 2 members */ }
#[tokio::test] async fn decline_refuses_and_writes_nothing() {}
#[tokio::test] async fn open_ticket_admits_without_prompt() {}
#[tokio::test] async fn forged_open_mode_is_refused() {}
#[tokio::test] async fn once_ticket_admits_once() { /* second candidate with the same once ticket, at a DIFFERENT listed member while the inviter is offline -> refused once the roster converges */ }
#[tokio::test] async fn forged_ticket_from_another_members_link_is_refused() { /* Y re-signs X's ticket_id+secret as open -> bad_ticket, nothing admitted */ }
#[tokio::test] async fn candidate_field_must_be_the_peer_key() {}
#[tokio::test] async fn sixth_open_request_for_one_inviter_is_refused() {}
#[tokio::test] async fn abandoned_requests_expire_and_free_the_limit() { /* 5 requests whose candidates disconnect; +600 s: rows expired; a 6th is accepted */ }
#[tokio::test] async fn link_replaced_on_removal_no_longer_joins() {}
#[tokio::test] async fn cloud_candidate_without_bridge_proof_is_refused() {}
#[tokio::test] async fn non_admin_inviter_admits_its_own_invitee_with_no_admin_online() { /* founder offline; member X mints `invite --new`; C joins via X's link; X's dialog -> Allow; C admitted; founder back online converges */ }
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: admission with a person's allow`.

### Task 7: Presence, directory, labels

**Files:** Create `src/presence.rs`, `src/label.rs`. Modify `src/net/mod.rs`, `src/daemon/net_ops.rs` (`team.get` members gain `online`), `src/daemon/ipc.rs` (`register` and named `inbox` consult `kept`), `src/frame.rs`, `src/cli.rs` (`who --all`). Test: `src/label.rs`, `tests/directory.rs`.

**Interfaces:**
- `Presence` fields: `sessions: [Remote {session_id, agent, repo, branch, folder, status, doing, tier, title}]` (`folder` = last path part unless `share_paths`; `folders_off` never published); `known` = this computer's non-live sessions that still hold waiting mail; `kept` = session names reserved 24 h after ending, which no new session on the same person's other computers may take: `register` (and `listen --as`) refuses `name_taken` when the latest presence of another computer of this `person_id` keeps the name (best effort). Sent on connect and on change; a lower `seq` is ignored; stored in `peers_sessions`; dropped after 24 h offline. `from_peer_ok`: `presence.member` and every id in it carry the peer's prefix.
- Produces: `label::render(roster, viewer_person: &str, from: &str) -> String`, from the roster only: a session of the same `person_id` → `Your <Agent>`; another person's session → `<name>'s <Agent>`; own `/terminal` → `You, at the terminal`; another member's `/terminal` → `<name>, at the terminal`; `<member>/<mailbox>` → `<name>'s <mailbox> mailbox`; a label naming an unverified member ends with ` (not verified)`. `frame::framed` takes the rendered label instead of `person: Option<&str>` (Plan A tests kept green).
- Op `directory {query?}` → `{sessions: [record]}`, record = architecture §9 `{session_id, person, person_verified, computer, agent, cwd, repo, branch, status, doing, tier, can_send, needs_owner_ok}`; `cwd` empty for remote sessions; `needs_owner_ok` = the session's `person_id` differs from the caller's (the recipient's policy is not visible to the sender).

- [ ] **Step 1: Failing tests**
```rust
#[test] fn labels_follow_the_roster() { assert_eq!(render(&r, &a_pid, "bshort/codex:1"), "aden's Codex (not verified)"); assert_eq!(render(&r, &a_pid, "bshort/terminal"), "aden, at the terminal (not verified)"); assert_eq!(render(&r, &a_pid, "bshort/desk"), "aden's desk mailbox (not verified)"); }
#[tokio::test] async fn each_member_sees_the_whole_directory() {}
#[tokio::test] async fn presence_naming_another_prefix_is_dropped() {}
#[tokio::test] async fn kept_name_is_refused_on_the_persons_other_computer() {}
#[tokio::test] async fn off_folder_sessions_are_not_published() {}
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: presence, directory, roster labels`.

### Task 8: Envelopes, the outbox, remote receipts, `leave` to peers

**Files:** Create `src/outbox.rs`. Modify `src/mail.rs`, `src/daemon/ipc.rs` (`send`), `src/daemon/net_ops.rs` (`team.leave`), `src/net/mod.rs`. Test: `src/outbox.rs`, `tests/envelope.rs`.

**Interfaces:**
- Produces: `mail::prepare(store, from, to, text, request_id, reply_to, now) -> Result<Prepared, Receipt>` (request_id format, idempotency and `request_id_reused`, loop check, thread and hops with `MAX_HOPS = 4`). **Hops follow the trigger** (architecture §7; Codex, #4297): with `reply_to`, `hops = that message's hops + 1` and its `thread`; without it, the trigger is the newest message delivered to the sending session from any session within the last 600 s (by delivery time): `hops = trigger.hops + 1`, and the `thread` is the trigger's when the destination is the trigger's sender (an implicit reply), a new one otherwise (a forward); no trigger → `hops = 0`, new `thread`. A `/terminal` sender (a person) is always `hops = 0`. The same rule for local and remote recipients, so a chain that hops across sessions without `reply_to` still stops at 4; Plan A's `mail::send` keeps its signature and calls it; `outbox::enqueue(store, prepared: Prepared, to_member: &str) -> Result<Receipt>` (receipt `queued`, `where` = the member's computer). `send` IPC: a `to` whose prefix is not ours takes the outbox path; optional `kind` (`message` only; others `unsupported`).
- Produces: `outbox::due(store, now)`, `outbox::acked(store, member, envelope_id, state, reason)` — **rank-checked: a receipt state only ever moves forward** in the order `queued < stored < delivered < read` (terminal `held`/`refused`/`expired` never regress to a lower rank either), so a delayed duplicate `Ack` re-sent from `seen_ids` after a lost first ack cannot pull a `delivered` back to `stored` (Codex round 4); `seen_ids.state` is updated as the message advances so a re-ack carries the newest state — `outbox::expire(store, now)` (24 h → `expired` `unreachable`); flush on peer connect and on a tick (1 s doubling to 60 s). Debug `LIGHTBULB_NETWORK_DEBUG_DROP_ACKS=1` drops inbound acks.
- Produces: `mail::receive(store, env, peer: &Member, roster, now) -> Result<(State, Option<String>)>`: `from` prefix == `peer.short` (else drop, no ack); `kind != "message"`, a non-empty `files`, or a body over 64 KB → `refused unsupported` (checked before dedupe, limits and policy; Codex round 3); group `to` → `refused policy`; recipient member not us → `refused not_member`; `(peer, id)` in `seen_ids` → re-ack the stored state, deliver nothing; `request_id` reused with other text → `refused request_id_reused`; `hops >= 4` or 12/600 per principal → `refused loop`; then policy (Task 9; until then `stored`) into the mailbox with `origin = "net:<short>"`. Replies `Ack`; later changes (`delivered`, `read`, `expired`, Task 9 outcomes) queue a `Receipt` in `receipt_out`, flushed on connect. `from_peer_ok`: `Ack`/`Receipt` only from the outbox row's `to_member`.
- `team.leave` (completing Task 3) sends the `leave` entry to at least one online peer, or after 5 s leaves anyway and prints `Could not tell the team; they will see you gone when you are removed.`

- [ ] **Step 1: Failing tests**
```rust
#[tokio::test] async fn send_to_teammate_walks_queued_stored_delivered_read() {}
#[tokio::test] async fn offline_recipient_stays_queued_then_stores_on_reconnect() {}
#[tokio::test] async fn outbox_survives_restart_and_dedupes() { /* DROP_ACKS on A; restart A without it; B delivered exactly once; duplicate re-acked from seen_ids */ }
#[tokio::test] async fn outbox_expires_after_24h_unreachable() {}
#[tokio::test] async fn late_duplicate_ack_never_regresses_a_receipt() { /* first ack dropped; recipient reads the message; resend -> re-ack; sender's receipt stays read */ }
#[tokio::test] async fn envelope_from_another_members_prefix_is_dropped() {}
#[tokio::test] async fn ack_from_a_third_member_is_ignored() {}
#[tokio::test] async fn fourth_hop_and_thirteenth_send_are_refused_loop() { /* both with reply_to and as a forward chain without reply_to within 600 s */ }
#[tokio::test] async fn a_person_at_the_terminal_resets_hops_to_zero() {}
#[tokio::test] async fn input_kind_is_refused_unsupported() {}
#[tokio::test] async fn files_or_oversize_body_are_refused_unsupported() { /* files: ["x"]; body of 65 537 bytes; both refused before any store/hold */ }
#[tokio::test] async fn implicit_reply_keeps_the_trigger_thread() { /* B replies to A without reply_to within 600 s -> same thread, hops+1; B forwards to C -> new thread, hops+1 */ }
#[tokio::test] async fn leave_reaches_a_peer_or_warns_after_5s() {}
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: envelopes, outbox, remote receipts, leave to peers`.

### Task 9: Policy and holds

**Files:** Create `src/policy.rs`, `src/holds.rs`. Modify `src/mail.rs` (`receive` consults policy), `src/daemon/net_ops.rs`, `src/cli.rs`. Test: `src/policy.rs`, `tests/holds.rs`.

**Interfaces:**
- Produces: `policy::Policy { people, cloud_of, agents_send_without_asking, set_by }`, `policy::decide(p, me: &Member, from: &Member) -> Verdict { Accept, Hold, Refuse }`; ops `policy.get {}`, `policy.set {person_id, scope: "people"|"cloud_of", value}` or `{agents_send_without_asking}` (person-only).
- Produces: `holds::Scope { Once, Person, OwnCloud, CloudOf }` (the one enum, reused by Task 14); `holds::hold(store, msg, now) -> Hold` (receipt `held`); `holds::answer(store, id, verdict, scope: Scope, by: "local"|"bridge", now) -> Result<()>`: `not_open` when already answered; `OwnCloud` only for the person's own cloud member, `CloudOf` only for another person's cloud member, else `bad_request`; Allow → mailbox, receipt `stored`, then the normal path; decline → `refused declined`; a signed `approvals` record keeps the text shown; `holds::expire(store, now)`: a hold whose target session has been gone (ended or absent from `sessions`) for 24 h → `expired` reason `session_ended`, and 7 days is the absolute maximum (→ `expired`); either withdraws the dialog (Codex, #4297). A held body is never delivered before Allow.
- Ops: `holds {}` → `{holds: [{id: "H<n>", target_session, from, label, preview, created_at}]}`; `hold.get {id}` (person-only, with body); `hold.answer {id, verdict, scope}` (person-only). Subscribers get `{event:"held"}` and `{event:"hold_closed", id, by}`; any answer withdraws the dialog.
- Dialog (no live claim): title `Lightbulb Network`, text `<label> wants to message your <Agent> in <folder>.\n"<first 160 chars, defanged>"`, buttons `Decline`, `Allow`, plus `Always allow` when the sender is a cloud member.
- CLI: `held`, `approve H<n> [--always]` (`--always` → `CloudOf` or `OwnCloud` by sender), `decline H<n>`, `policy [<person> accept|hold|refuse]` (no args lists, marking `set_by: bridge`).

- [ ] **Step 1: Failing tests**
```rust
#[test] fn defaults_accept_own_person_hold_others() {}
#[tokio::test] async fn teammate_first_message_is_held_then_allow_delivers_and_writes_accept() { /* approve H1 -> stored -> delivered; next message from another aden session stored directly */ }
#[tokio::test] async fn decline_refuses_and_delivers_nothing() {}
#[tokio::test] async fn second_answer_is_not_open() {}
#[tokio::test] async fn hold_expires_after_7_days() {}
#[tokio::test] async fn hold_expires_24h_after_its_target_session_ends() { /* end the target session; +24h: receipt expired/session_ended; a later approve -> not_open; nothing in the mailbox */ }
#[tokio::test] async fn always_on_a_computer_sender_is_bad_request() {}
#[tokio::test] async fn approve_from_agent_shell_is_refused() { /* CALLER_ENV names a claude ancestor -> hold.answer error not_a_person */ }
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: inbound policy, holds, dialog, approve/decline`.

### Task 10: MCP and CLI over the network; `folder`, `reload`

**Files:** Modify `src/mcp.rs`, `src/cli.rs`, `src/main.rs`, `src/daemon/net_ops.rs`, `src/daemon/ipc.rs` (`register` ignores a cwd under `folders_off`). Test: `tests/mcp_tools.rs`, `tests/cli.rs`.

**Interfaces:**
- `find_sessions` reads `directory`; `check_inbox` and `inbox` records become `{id, request_id, thread, from, label, person, person_verified, computer, text, sent_at}`.
- `send_message` to a remote `session_id` returns by receipt: `stored on <computer>. It may be held for <name> to approve; you will see the receipt change.`; `queued: <computer> is offline. Will retry until <time, +24h>.`; held → `held: waiting for <name> to approve`. Groups stay `group_send_not_allowed`.
- CLI: remote `send` prints `<id> stored on <computer> (tier <tier>). Receipt: lightbulb-network receipt <id>` or the queued line; `who --all`; `folder <path> on|off` (writes `config.toml`, then `reload`); op `reload {}` re-reads `config.toml` into `TeamState` (and `relays.json` after Task 12).

- [ ] **Step 1: Failing tests**
```rust
#[tokio::test] async fn find_sessions_lists_teammates_with_person_and_computer() {}
#[tokio::test] async fn send_message_to_teammate_says_where_it_waits() {}
#[tokio::test] async fn check_inbox_record_names_person_and_computer() {}
#[test] fn folder_off_hides_sessions_after_reload() {}
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: mcp and cli across members; folder on/off; reload`.

### Task 11: Streams and `share_grant`

**Files:** Create `src/streams.rs`. Modify `src/net/mod.rs`, `src/daemon/net_ops.rs`. Test: `tests/streams.rs`.

**Interfaces:**
- Ops: `share.grant {session, viewer, input: false, expires_at}` → `{grant_id}` (person-only; a signed `ShareGrant` for this computer's own session, sent to the viewer, stored in `grants` on both sides; `input: true` → `unsupported`, Plan D); `stream.open {session}` → `{stream_id}` (viewer side: a QUIC bidirectional stream to the session's member, opened by `Stream {grant_id, session, seq: 0, bytes: ""}`; the owner accepts when the viewer's `person_id` equals its own or a live grant names the viewer, else `no_grant`); `stream.send {stream_id, data_b64}` (owner side, ≤ 64 KB decoded); `stream.close {stream_id}`. Events: `stream_opened {stream_id, viewer}`, `stream {stream_id, seq, data_b64}`, `stream_closed {stream_id}`. QUIC flow control is the backpressure. In Plan B the owner DROPS every viewer→owner `Stream` frame after the opener (input is Plan D).

- [ ] **Step 1: Failing tests**
```rust
#[tokio::test] async fn same_person_opens_without_grant() {}
#[tokio::test] async fn teammate_without_grant_gets_no_grant() {}
#[tokio::test] async fn granted_viewer_receives_bytes_in_order_until_expiry() {}
#[tokio::test] async fn viewer_bytes_never_reach_the_owner() {}
#[tokio::test] async fn chunk_over_64k_is_bad_request() {}
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: unicast streams on share grants`.

### Task 12 (deferrable: waits on the owner's relay spend): Relay fallback

**Files:** Create `src/relay.rs`. Modify `Cargo.toml` (`minisign-verify`), `src/net/mod.rs`, `src/cli.rs` (`relay [<url>]`), `src/ticket.rs` (fill `relay_hint`). Test: `tests/relay.rs` (iroh's own test relay server).

**Interfaces:**
- `relay::list(dir, config, roster) -> Vec<Url>`: `config.relay` or the roster's `meta.relay` (a team's own) wins, and then nothing of ours is contacted; else `relays.json` verified against one of two compiled-in minisign keys and accepted only whole. iroh runs with these relays (hole punching comes with them). `relay <url>` writes `config.toml`; from the founder or an admin it also writes `meta {relay}` so the whole team adopts it; `relay` alone prints the list. `status.relay` = `ok|unreachable`, `status.peers.direct`. Reconnect through each relay, backoff ≤ 60 s. Nothing is ever sent in plaintext.

- [ ] **Step 1: Failing tests**
```rust
#[tokio::test] async fn messages_flow_via_relay_when_direct_is_blocked() {}
#[tokio::test] async fn relay_down_keeps_mail_queued() {}
#[test] fn unsigned_or_partial_relay_list_is_refused() {}
#[tokio::test] async fn team_relay_from_meta_replaces_ours() {}
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: relay fallback with a signed relay list`.

### Task 13 (deferrable: needs the issuer route and keys): Sign-in proofs, cloud and same-person admission

**Files:** Create `src/proof.rs`. Modify `Cargo.toml` (`ureq`), `src/roster.rs` (`merge`, `proof`, same-person `admit`), `src/admission.rs`, `src/label.rs`, `src/cli.rs` (`signin`, `unlink`, hidden `join --kind cloud`), `src/daemon/net_ops.rs`. Test: `src/proof.rs`, `tests/signin.rs`.

**Interfaces:**
- `proof::Proof { iss, kid, iat, exp, sub, name, computer, kind: "computer"|"bridge", workspace: Option<String> }` (compact JWS, header `{alg:"EdDSA", kid}`, ≤ 4 KB, 24 h); `proof::verify(token, member_key, now) -> Result<Proof>` offline against `ISSUER_KEYS: [(&str, [u8; 32]); 2]` (the owner supplies them) plus, under `#[cfg(debug_assertions)]`, one compiled-in test issuer key; `computer` must equal `member_key`.
- `signin` (these routes are the contract Plan C implements): `POST {issuer}/device/start {member_key, computer}` → `{device_code, user_code, verification_uri_complete, interval}`; `open <uri>`; poll `POST {issuer}/device/token {device_code}` → `{proof}`; then `identity.proof {proof}` writes `merge` (when `person_id` is still the own key) or `proof` (a renewal) to the roster. Proofs live only in the roster; expiry clears the verified badge, never `person_id`.
- Same-person: a `join_request` whose proof `sub` is an existing `person_id` is forwarded to an online computer of that person, which asks `Your new Mac <name> wants to join. Allow?` and signs `admit {person_id: sub, proof}`; no such computer online → `JoinDecision {verdict: "decline", reason: "your_computer_offline"}`, candidate prints `Open your other computer, then run the same command again.`
- Cloud: a `kind: "cloud"` candidate is admitted (founder or admin, on Allow) only with a valid `kind=bridge` proof, recorded in its `admit`. A cloud member never mints a link, never admits, and `team.invite` from one is refused `not_admin` (Task 2 drops such entries on replay anyway; Codex round 2).
- Labels read the roster: verified names drop ` (not verified)` and show `✓ <proof name>`; a session of a bridge-role member whose proof `sub` = X's `person_id` renders `<App> via <X's name>` (`<App>` ∈ `Claude, ChatGPT, Muse, Grok Bot, Cursor, Codex, Friday`, else `App "<sanitised ≤32>"`). `team.unlink {}` (person-only) writes `remove` of this person's bridge member.

- [ ] **Step 1: Failing tests**
```rust
#[test] fn proof_verifies_offline_and_rejects_wrong_computer_or_expired() {}
#[test] fn merge_switches_person_id_once_and_expiry_keeps_it() {}
#[tokio::test] async fn second_computer_of_same_person_is_admitted_by_the_first() {}
#[tokio::test] async fn cloud_member_joins_with_a_bridge_proof() { /* third engine, join --kind cloud, test-issuer proof */ }
#[test] fn bridge_session_renders_app_via_name() {}
#[tokio::test] async fn unlink_removes_only_own_persons_bridge() {}
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: sign-in proofs, cloud and same-person admission`.

### Task 14 (deferrable, after 13): `hold_notice` and relayed `decision`

**Files:** Create `src/decision.rs`. Modify `src/holds.rs`, `src/net/mod.rs`. Test: `tests/decision.rs` (fixture: a third engine admitted with `join --kind cloud` and a test-issuer `kind=bridge` proof, Task 13).

**Interfaces:**
- On every new hold, send `HoldNotice` to this person's bridge member only (found in the roster), THROUGH THE OUTBOX (Task 8): it retries until the bridge acks it or the hold resolves or expires, so a bridge that is offline when the hold is created still learns of it on reconnect (Codex round 3); `preview` (≤160 defanged chars) only if `config.preview_to_workspace`.
- `decision::accept(store, roster, signer: &Member, d: &Decision, now) -> Result<()>` calls `holds::answer(…, by: "bridge")` only if: `sig` valid; the signer's ROSTER proof is `kind=bridge` with `sub` = the hold target's `person_id`; `hold_id` and `holding_member` (us) match; nonce unseen; scope `Once` or `Person` for a held message from ANY sender (the phone/web Allow of a teammate's message is the point of the flow); `OwnCloud` only when the held message came from that bridge's own session; `CloudOf` is refused (Codex, #4297). A held message from the bridge's own session may be allowed by it (architecture §7 is binding over the engine spec's "no self-approval"). A relayed `Person` is stored `set_by: bridge`; the CLI shows `Allowed <name> from your phone`.

- [ ] **Step 1: Failing tests**
```rust
#[tokio::test] async fn bridge_allow_once_delivers_and_writes_no_policy() {}
#[tokio::test] async fn decision_from_another_persons_bridge_is_refused() {}
#[tokio::test] async fn relayed_cloud_of_is_refused() {}
#[tokio::test] async fn replayed_nonce_is_refused() {}
#[tokio::test] async fn own_cloud_scope_only_for_the_bridges_own_session() {}
#[tokio::test] async fn preview_absent_unless_configured() {}
#[tokio::test] async fn hold_notice_reaches_a_bridge_that_was_offline() { /* bridge down at hold time; comes up; notice delivered; a hold resolved meanwhile sends nothing */ }
```
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat: hold notices and relayed decisions`.

### Task 15: Two-Mac acceptance

**Files:** Create `docs/acceptance-plan-b.md` (rows, commands, receipts, PASS/FAIL, build sha). Tag `v0.2.0` after a full PASS.

- [ ] **Step 1 (first pass, this Mac, two state dirs, a DEBUG build so `LIGHTBULB_NETWORK_PEERS` works):** `cargo build`. A = the real install (wired agents), restarted on the debug build with `LIGHTBULB_NETWORK_PEERS=<B key>@127.0.0.1:<B port>`. B: `mkdir -p /tmp/lbn-b /tmp/lbn-b-claude; printf 'computer = "mac-b"\n' > /tmp/lbn-b/config.toml`, then `LIGHTBULB_NETWORK_HOME=/tmp/lbn-b LIGHTBULB_NETWORK_CLAUDE_HOME=/tmp/lbn-b-claude LIGHTBULB_NETWORK_PEERS=<A key>@127.0.0.1:<A port> target/debug/lightbulb-network daemon &`. On A `invite`; on B `join '<line>' --name aden` → A's dialog → Allow; B prints `Connected as aden (mac-b) …` (row 10). B `send <A's claude session> "hi"` → `held` + dialog (row 2); Allow → delivered, framed `aden, at the terminal (not verified)`; a second B send goes straight in (row 3); a fresh one Declined → `refused declined` (row 4); stop B's daemon, A sends to `<B>/desk` → `queued`; restart B (same team, no new genesis: Task 3), `listen --as desk --once` gets it, A's receipt `stored` → `delivered` (row 9); B `leave` → B's `team` lists one member and A's directory drops B (row 10; needs Task 8).
- [ ] **Step 2 (two Macs, same Wi-Fi; the teammate follows this numbered script on Mac B, a different macOS account and person, release build):**
  1. Copy `lightbulb-network` to `~/.lightbulb/network/bin/`, run `~/.lightbulb/network/bin/lightbulb-network setup`, restart Claude Code and Codex.
  2. Paste the join line you were sent into Claude Code and ask it to run it, or run `lightbulb join '<line>' --name <you>`; wait for `Connected as …` (A clicks Allow).
  3. In Claude Code: "find the sessions of `<A's name>` and send their Claude `ping from B` with send_message"; read the receipt with message_receipt and text it to A.
  4. After A says "allowed", send a second message from Codex; confirm the receipt reads `stored` or later without A doing anything.
  5. Send a third; A declines it; confirm `refused`, reason `declined`.
  6. Close the lid for 2 minutes while A sends to your Claude; open it; confirm the message arrives and A's receipt reads `delivered`.
  7. Run `lightbulb leave`, answer `y`; confirm `lightbulb team` lists only you.
- [ ] **Step 3 (A's side):** record each row with the dialog text, labels and receipt JSON; grep both `daemon.log`s for the unique body markers (must be absent).
- [ ] **Step 4 (when Task 12 lands):** repeat rows 2 and 9 with B on a phone hotspot.
- [ ] **Step 5:** Commit `docs: plan b acceptance`, PR, Codex review, merge, tag.

## Self-review

- **Spec coverage.** §3 identity (T1, T13); §4 roster, roles, transfer, succession, link, tickets, `--open/--once/--new`, same-person and cloud admission, `unlink`, one network per computer (T2, T3, T6, T13); §5 prefix collisions, labels, names (T2, T6, T7, T13); §6 every frame, defined once (T4) and used in T6–T9, T11, T14; §7 outbox, holds, policy, the scope enum, decisions, `ui.claim`, limits, hops (T5, T8, T9, T14); §8 streams (T11); §9 the B ops, records, `kind` (T1, T3, T6–T11). Left out on purpose: file attachments (`files` crosses empty; non-empty stays `unsupported` until a follow-up task), `init` (the solo network replaces it), self-update, `doctor` clock-skew checks.
- **Type consistency.** `Identity`, `TeamState` (T1), `Entry`/`Roster`/`Member`/`Role`/`MemberKind` (T2), `Ticket` (T3), `Frame` (T4, the only definition), `Presence`/`Remote` (T7), `Prepared` (T8), `Policy`/`Verdict`/`Hold`/`Scope` (T9, reused by T14), `Proof` (T13) are each defined once. Plan A's `Key`, `Session`, `Message`, `Receipt`, `State` and `mail::send`'s signature are unchanged; `frame::framed` changes one parameter (T7).
- **Proportion.** 15 tasks; 12, 13 and 14 can wait without breaking the rest. No function bodies. The one ponytail cut left: transient frames are unsigned (the QUIC peer key stands in).
