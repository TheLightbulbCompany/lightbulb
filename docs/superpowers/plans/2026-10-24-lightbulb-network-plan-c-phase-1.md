# Lightbulb Network Plan C, Phase 1: the Workspace Member in the Runtime (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each workspace one more computer on the `lightbulb` network. The engine runs as a sidecar in the workspace's AgentCore microVM, Friday and the other cloud agents are its declared sessions, and a Mac joins by pasting one line from Settings.

**Architecture:** The engine (`TheLightbulbCompany/cli`, v0.3.0) gains a headless `lightbulb member` subcommand that wraps `embed::start` and adds a token-gated Unix socket. The Hermes bridge starts it on the first real `/invocations`, restores and saves `<owner>/network/state.db` with conditional puts, turns inbound network messages into local run-less turns, and serves the backend's network calls as RPC methods on the existing `/plugins/lightbulb-channel/rpc` path. The backend mints the member key into the existing boot secret, writes `<owner>/network/sessions.json`, and reuses the webhook-agent Needs-you items and the hosted MCP.

**Tech Stack:** Rust 1.91 (iroh 1.4.1, tokio, rusqlite), Python 3 bridge (boto3, stdlib), FastAPI + DynamoDB backend, Next.js 16 + vitest, AWS CDK.

**Spec:** `docs/superpowers/specs/2026-10-23-lightbulb-network-stateless-cloud-member-design.md` (rev 3.1). §4.4 is the bridge↔engine contract, §4.5 the jobs, §4.6 the doorbell, §7 the engine changes, §11 the task list.

**Scope:** Phase 1 is messages only. Session streams to the web and phone are phase 2. Phase 2 starts right after phase 1 ships and uses the same member path.

**Owner decisions, 2026-10-24 (no task is conditional):** (a) the doorbell is in (E4 in Task 4, the cloud half in Task 12). (b) Every workspace gets a member, minted at provision and reconcile (Task 8). (c) The founder-key residual risk (spec §9) is accepted, so there is no task for it. The phase 3 hardening is key rotation (`--new-key`), verified names (the issuer), and a key the agents cannot read.

**Two deviations from the spec, both reuse:**
- The spec's `/network/<x>` invoke paths are RPC methods `network.<x>` on the existing RPC path. This keeps one envelope, one gateway-token check, and `container_rpc.call` with its 90 s floor.
- There is no `network-version.json` and no Rust build stage. The Dockerfile downloads the engine's own `lightbulb-linux-arm64.tar.gz` release asset, pinned by tag and sha256 inline, the same way it pins `gog@v0.39.1`. Add a JSON pin when a second consumer reads the tag.

## Global Constraints

- The backend never sees network message bodies, hold text, the roster or keys (spec §3). Hold notices carry only the hold id, the sender label and the target session.
- Only `<owner>/network/state.db` is persisted to S3 (plus the backend-written `<owner>/network/sessions.json`). `addrs.json` and `identity.key` are never uploaded.
- The member key leaves the boot secret only as the sidecar's `identity.key` (raw 32 bytes, mode 0600) in `/opt/data/network`. No other backend code reads `networkKey`.
- Anything a person reads says "messages", never "mail".
- The public engine repo carries no internal hostnames, account ids or Lightbulb backend URLs. The doorbell URL arrives at runtime in the sessions file.
- The engine stays macOS-first. It also builds, tests and releases on linux/arm64 (the AgentCore microVM) and linux/x86_64 (the owner's WSL computer). There is no Windows-native port. Homebrew and npm stay macOS-only for now.
- Limits: `presence::MAX = 100` sessions (agents plus persons × apps); `TTL_S = 86_400`; `MAX_HOPS = 4`; `LOOP_LIMIT = 12` per 600 s; `SIGTERM_SAVE_S = 10.0`; the network stop waits ≤ 6 s for the final snapshot.
- The backend reaches the runtime only through `container_rpc.call` or `runtime_transport.request`. Every session stop goes through `hermes_provisioning.stop_runtime_session`.
- Engine paths in the image: home `/opt/data/network`, snapshot `/opt/data/network-out/state.db`, socket `/opt/data/network.sock`, binary `/usr/local/bin/lightbulb`.

## Review Focus

1. **A turn that dies, or is refused by a recycle hold.** The message must run again on the next start and must never be lost. Pinned in Task 6: `test_a_recycle_hold_leaves_the_message_unacked` and `test_a_turn_that_raises_is_not_acked_read`.
2. **SIGTERM during a debounced save.** The network stop must not starve the profile saves, and must finish inside `SIGTERM_SAVE_S`. Pinned in Task 5: `test_sigterm_runs_the_network_stop_beside_the_profile_saves`.
3. **Two concurrent first `/invocations`.** Exactly one sidecar may start. Pinned in Task 5: `test_concurrent_first_invokes_start_one_sidecar`.
4. **A hosted assistant retrying after a 504 with the same `request_id`.** Exactly one network message may go out. Pinned in Task 10: `test_a_retry_with_the_same_request_id_sends_once`.
5. **A reprovision that keeps an old `state.db`.** The next start must not hit `KEY_MISMATCH`. Pinned in Task 8: `test_teardown_deletes_network_state_even_without_delete_profiles`.

---

### Task 1: Engine E0, the Linux port (arm64 and x86_64), Linux CI and Linux release assets

**Files:**
- Modify: `lightbulb-network.nosync/.github/workflows/ci.yml` (matrix `os: [macos-latest, ubuntu-24.04-arm, ubuntu-24.04]`; delete the "Plan A is macOS-only" comment)
- Modify: `.github/workflows/release.yml` (two new jobs beside `macos-arm64`: `linux-arm64` on `ubuntu-24.04-arm`, target `aarch64-unknown-linux-musl`, and `linux-x86_64` on `ubuntu-24.04`, target `x86_64-unknown-linux-musl`. Each runs `sudo apt-get install -y musl-tools`, `rustup target add <t>`, `cargo build --release --target <t>`, and uploads `lightbulb-linux-<arch>.tar.gz` + `.sha256` to the same release. `publish-npm` and Homebrew still `needs: macos-arm64` only.)
- Modify: `install.sh:9,17-18` (choose `ASSET` by `uname -s`/`uname -m`: `Darwin arm64` → `lightbulb-macos-arm64.tar.gz`, `Linux aarch64` → `lightbulb-linux-arm64.tar.gz`, `Linux x86_64` → `lightbulb-linux-x86_64.tar.gz`, and anything else exits 1 with "Lightbulb runs on Apple-silicon Macs and on Linux (arm64, x86_64)." On Linux, `setup` runs with no background service, and the script prints "Start it with: lightbulb daemon (or as a systemd --user service).")
- Modify: `src/cli.rs` (`daemon --install`/uninstall, `launchctl`, `Library/LaunchAgents`, the Local Network doctor row at :1467, all behind `#[cfg(target_os = "macos")]`; on Linux, `daemon --install` exits 2 with "Lightbulb's background service is macOS-only; run lightbulb daemon.")
- Modify: `src/dialog.rs` (`Queue::new` on non-macOS returns `Queue::headless()`; `osascript` only on macOS. On Linux, holds are answered with `lightbulb approve`/`decline`.)
- Modify: `src/os.rs:144-160` (Linux, not "non-macOS": `peer_pid` through `getsockopt(SO_PEERCRED)`, and `info`/`executable` from `/proc/<pid>/stat` and `/proc/<pid>/exe`, so a person at a Linux terminal is a person; other targets keep the error stubs)
- Modify: `README.md`, `docs/releasing.md` (one line each: the Linux assets and the Linux install, with Homebrew/npm macOS-only for now)
- Test: only the dialog and launchd tests get `#[cfg(target_os = "macos")]` (`tests/dialog.rs`, the launchd cases in `tests/cli.rs`). Person-at-the-socket tests (`tests/team_cli.rs`, `tests/ui_claim.rs`, `tests/holds.rs`) run on Linux too. The `src/os.rs` and `src/daemon/net_ops.rs:813` unit-test modules widen from `target_os = "macos"` to `any(target_os = "macos", target_os = "linux")`.

**Interfaces:**
- Produces: release assets `lightbulb-linux-arm64.tar.gz` (static musl, which Task 5 downloads) and `lightbulb-linux-x86_64.tar.gz` (the WSL computer in Task 13), each with a `.sha256`. No API change.

- [ ] **Step 1:** Add the two Linux legs to the matrix, push a branch, and read the failing jobs. Expected: compile or test failures that name launchd, `osascript` or `peer_pid`.
- [ ] **Step 2:** Gate the launchd and dialog sites, and implement the Linux `os.rs` lookups. Do not change any path or behaviour on macOS. `presence.rs:117` is already gated.
- [ ] **Step 3:** Run `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test` on all three legs. Expected: green. `person_only_accepts_people_and_refuses_agents` passes on Linux.
- [ ] **Step 4:** Edit `release.yml` and `install.sh`. Test them on a pre-release tag: `git tag v0.3.0-rc.1 && git push origin v0.3.0-rc.1`, then on an Ubuntu x86_64 box run `curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/download/v0.3.0-rc.1/install.sh | LIGHTBULB_VERSION=v0.3.0-rc.1 sh && lightbulb version`. Expected: the checksum line passes and `lightbulb version` prints the rc version. Delete the rc release afterwards.
- [ ] **Step 5:** Commit `feat: Linux (arm64, x86_64) build, test and release (E0)`.

Skipped: a systemd unit installer, a Windows-native port, and Linux Homebrew/npm. Add them when a Linux user needs more than `lightbulb daemon`.

### Task 2: Engine E1 and E2, the `lightbulb member` sidecar and inbox peek/ack

**Files:**
- Modify: `src/main.rs` (new `Command::Member { home, snapshot, socket, name, relay: Option<String>, sessions }`)
- Create: `src/member.rs` (the sidecar: `pub async fn run(opts: MemberOpts, token: String) -> Result<()>`)
- Modify: `src/lib.rs` (`pub mod member;`)
- Modify: `src/embed.rs` (`EmbedOpts.sessions: Vec<Value>`, declared before `Net::start`)
- Modify: `src/daemon/net_ops.rs` (extract `cloud_sessions_set`'s validation into `pub(crate) fn remotes(short: &str, list: &[Value]) -> Option<Vec<Remote>>`, used by both the op and `boot`)
- Modify: `src/daemon/ipc.rs` (`inbox.peek` and `inbox.ack` in `op`)
- Modify: `src/net/mod.rs` (at :815, when a peer closes our link with `SUPERSEDED`, send `{"event":"superseded","by_peer":<short>}` on `daemon.events`)
- Modify: `tests/common/mod.rs` (`instance()` passes `sessions: vec![]`; new `pub fn member(dir: &Path, sessions: Value) -> (Child, String /*token*/, PathBuf /*socket*/)` and `pub fn line(sock: &Path, token: &str, op: &str, body: Value) -> Value`)
- Test: `tests/member.rs`

**Interfaces:**
- Produces, CLI: `lightbulb member --home <dir> --snapshot <file> --socket <path> --name <ws name> --sessions <file> [--relay <url>]`. The token is the first line of stdin.
- Produces, sessions file: `{"workspace":"<name>", "sessions":[{"name","agent","title"?, ...}], "wake"?: {"url","token"}}` (`wake` is read in Task 4). Session fields other than `name`/`agent`/`title` are ignored. The bridge keeps `agentId`, `app` and `person` there for itself.
- Produces, socket: one JSON object per line, `{"token", "id", "op", ...body}` (`id` is the line's request id, a u64). The op runs as `Caller::Instance` and the answer is `{"id": <line id>, "ok": true, "result": {...body...}}` or `{"id": <line id>, "ok": false, "error": "<code>", ...}` — the body is never merged into the envelope (ruled 2026-10-24: the daemon socket's merged `Response` shape collides on `id` for acks, holds and receipts). A missing or wrong token gets `{"id","ok":false,"error":"unauthorized"}` and the connection closes. `{"op":"subscribe"}` streams event lines `{"event":...}`. `{"op":"stop"}` (or SIGTERM) runs `Instance::shutdown` and exits 0. The `shutdown` op stays refused.
- Produces, ops: `inbox.peek {}` → `{"sessions":[{"session":"friday","records":[<INBOX_RECORD fields>]}]}`, built from `mail::waiting_keys` + `mail::waiting` + `ipc::records` (with `framed`), and taking nothing. `inbox.ack {session, record, stage:"delivered"|"read"}` → `{}` (`record` is the string record id; ruled 2026-10-24, it was `id`): `delivered` runs `mail::take_one`, `read` runs `mail::mark_read` then `mail::forget`. An unknown record gets `not_found`. Any other stage gets `bad_request`.
- Produces, event: `superseded {by_peer}`.

- [ ] **Step 1: Write the failing tests** in `tests/member.rs`:

```rust
#[test] fn a_line_without_the_token_is_refused_and_closed()      // error == "unauthorized"
#[test] fn status_answers_as_an_instance_and_shutdown_is_refused() // ok; "shutdown" -> unsupported
#[tokio::test] async fn an_early_message_to_a_declared_session_is_kept() // Mac sends to <short>/friday right after start: state "stored", not refused
#[tokio::test] async fn peek_lists_without_taking_and_ack_moves_the_receipt() // peek twice == same; ack delivered -> sender sees "delivered"; ack read -> "read" and the text is forgotten
#[tokio::test] async fn stop_writes_the_snapshot_outside_the_home()  // snapshot file exists, opens as a Store
#[tokio::test] async fn a_second_connection_with_this_key_emits_superseded() // dial_instance twice from one identity -> event "superseded"
```

- [ ] **Step 2:** Run `cargo test --test member`. Expected: FAIL (`member` is not a subcommand).
- [ ] **Step 3:** Implement `member::run`. Read the sessions file. Call `embed::start(home, EmbedOpts{kind: Cloud, relay, snapshot, name, sessions})`. Bind the socket with mode 0600, and remove a stale socket file first. Each connection forwards lines to `Instance::call` and forwards `Instance::events()` after `subscribe`. Compare tokens in constant time (`ed25519`'s `subtle` is not a dependency; compare with `a.len()==b.len() && a.bytes().zip(b.bytes()).fold(0,|x,(p,q)| x|(p^q))==0`).
- [ ] **Step 4:** Run `cargo test --test member && cargo test --test embed`. Expected: PASS. The existing embed tests still pass with `sessions: vec![]`.
- [ ] **Step 5:** Commit `feat: lightbulb member sidecar, sessions before start, inbox peek/ack (E1, E2)`.

Skipped: socket reconnect logic in the sidecar, because the bridge is its only client. Add it if a second client appears.

### Task 3: Engine E6 and E8, hold ops and person invites on the member

**Files:**
- Modify: `src/daemon/ipc.rs:166` (`NOT_ON_AN_INSTANCE` loses `team.invite`)
- Modify: `src/daemon/net_ops.rs:21-90` (`Invite::Person`; the `team.invite` gate admits `Caller::Instance` for `mode: "person"` only)
- Modify: `src/admission.rs:311-314` (a link with `mode == "person"` admits without an Allow, once, and writes PEOPLE `accept` for its `for`)
- Modify: `src/holds.rs:550-560` (`op` admits `Caller::Instance` for `holds`, `hold.get`, `hold.answer`); `:623` (`answer_here(.., by)` takes `body["decided_by"]` on the instance path; the dialog and CLI paths keep `"local"`)
- Modify: `src/ticket.rs` (`mode` accepts `"person"`; `for` rides as `#[serde(default)] pub person: Option<String>`)
- Test: `tests/member.rs` (append), `tests/holds.rs` (append)

**Interfaces:**
- Produces: `team.invite {mode:"person", for:"<clerk user id>", name:"<display name>"}` → `{line, expires_at}`. `line` is exactly `lightbulb join '<ticket>' --name '<name>'`. A name with `'` or a control character gets `bad_request`. `expires_at` = now + 86 400.
- Produces: `holds {}`, `hold.get {hold:"H<n>"}` (the existing shape, `body` included; Task 6 drops it), and `hold.answer {hold, verdict:"allow"|"decline", scope?, decided_by:"<clerk user id>"}` → `{id, verdict, scope}`. On an instance, a missing `decided_by` gets `bad_request`.

- [ ] **Step 1: Write the failing tests:**

```rust
#[tokio::test] async fn a_person_link_admits_a_mac_without_an_allow()        // join completes, no J row waits
#[tokio::test] async fn a_person_link_is_single_use()                         // second join -> refused
#[tokio::test] async fn the_invited_persons_first_message_to_friday_is_not_held() // receipt "stored"
#[tokio::test] async fn ask_and_open_links_still_refuse_on_an_instance()      // mode "open" -> unsupported/not_a_person
#[tokio::test] async fn a_strangers_first_message_is_held_and_answered_with_decided_by() // holds lists it; answer -> hold row by == "user_x"
#[tokio::test] async fn hold_answer_on_an_instance_needs_decided_by()          // bad_request
```

- [ ] **Step 2:** Run `cargo test --test member --test holds`. Expected: FAIL.
- [ ] **Step 3:** Implement as listed under Files. No new hold logic: `policy::decide` already holds.
- [ ] **Step 4:** Run `cargo test`. Expected: PASS, the Mac dialog and CLI hold tests unchanged.
- [ ] **Step 5:** Commit `feat: person invites and hold ops on the member (E6, E8)`.

Skipped: phone answers for holds on a Mac. That is phase 3.

### Task 4: Engine E4, E5 and E7: the doorbell, hosted apps on the member, member relays off; tag v0.3.0

**Files:**
- Modify: `src/app_send.rs` (rewrite: `app.send|app.inbox|app.receipt {app, person, ...}` run on the member from the declared session `{app}-{person}`; delete the relay to a person's computer, `WAIT`, `PROBE`, `NO_COMPUTER_ONLINE` and the `app_*` envelopes)
- Modify: `src/net/mod.rs:1029-1036` (an `app_*` envelope is dropped and logged); `:987-998` (the `decision` frames from the member are dropped)
- Modify: `src/decision.rs` (delete `hold_notice` sending, `decision.send` and `holds.pending`; keep nothing a computer still needs. If the file is empty, delete it and its `mod`)
- Modify: `src/daemon/ipc.rs:210-212`, `src/embed.rs` docs (drop the retired ops)
- Modify: `src/label.rs` (a session with a declared `title` of the form `<App> via <Person>` renders that title)
- Modify (E4, decided 2026-10-24): `src/roster.rs:532` (`Kind::Meta` body `{"wake":{"url","token"}}` from the founder → `Roster.wake: Option<Wake>`); `src/member.rs` (when the sessions file's optional `"wake"` differs from `Roster.wake`, sign that meta at start); `src/net/mod.rs` `keep_up` (after a failed direct dial to the founder while messages to it are due, run the system `curl -fsS -m 10 -X POST <url>` with backoff from 30 s to 15 min; the token is never logged)
- Modify: `Cargo.toml` (`version = "0.3.0"`), `docs/agents.md` (one paragraph on the member)
- Test: `tests/app_send.rs` (rewrite), `tests/decision.rs` (delete)

**Interfaces:**
- Produces: `app.send {app, person, to, text, request_id, reply_to?}` → the `send` receipt `{id, state, ...}`. `app.inbox {app, person}` → `{records}`, taken and read. `app.receipt {app, person, request_id}` → the receipt. An undeclared `{app}-{person}` gets `refused`, reason `not_declared`.
- Produces: app session name = `{app}-{person}`, where `person` is the opaque key the backend passes (Task 10). The title is `"<App> via <First name>"`.

- [ ] **Step 1: Write the failing tests** in `tests/app_send.rs`:

```rust
#[tokio::test] async fn an_app_send_goes_from_the_declared_app_session_with_the_mac_off_for_the_person()
#[tokio::test] async fn the_recipient_reads_it_as_app_via_person()       // label == "ChatGPT via Aden"
#[tokio::test] async fn an_undeclared_app_person_pair_is_refused()        // reason "not_declared"
#[tokio::test] async fn a_computer_ignores_app_envelopes_and_decisions_from_the_member()
// tests/member.rs (E4), with a fake `curl` first on PATH (common::script):
#[tokio::test] async fn a_computer_rings_the_doorbell_only_after_a_failed_dial()  // member up -> no curl; member down + message due -> one curl, then backoff
#[tokio::test] async fn the_doorbell_token_is_absent_from_logs()
```

- [ ] **Step 2:** Run `cargo test --test app_send`. Expected: FAIL.
- [ ] **Step 3:** Implement. Delete the superseded code in the same commit.
- [ ] **Step 4:** Run `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test` on both CI legs. Expected: PASS.
- [ ] **Step 5:** Commit `feat: the doorbell, hosted apps on the member, member relays off (E4, E5, E7)`, open the PR, merge on green, then `git tag v0.3.0 && git push origin v0.3.0`. Watch `release.yml` with `gh run watch <id> --repo TheLightbulbCompany/cli --exit-status --interval 60`. Expected: three platform assets on the release.

Skipped: an HTTP crate for the doorbell (the system `curl`, as `update` uses).

### Task 5: The engine in the Hermes image, and the `bridge/network.py` lifecycle

**Files:**
- Modify: `apps/infra/hermes/Dockerfile` (in the final stage, after the `gog` copy; `<sha256>` is the `.sha256` asset's value from Task 4's release):

```dockerfile
# The engine (TheLightbulbCompany/cli): its static musl release asset, pinned by tag and checksum.
RUN curl -fsSLo /tmp/lb.tgz https://github.com/TheLightbulbCompany/cli/releases/download/v0.3.0/lightbulb-linux-arm64.tar.gz \
 && echo "<sha256>  /tmp/lb.tgz" | sha256sum -c - \
 && tar -xzf /tmp/lb.tgz -C /usr/local/bin lightbulb && rm /tmp/lb.tgz && lightbulb version
```

- Create: `apps/infra/hermes/bridge/network.py`
- Modify: `apps/infra/hermes/bridge/__init__.py:56-64` (`NETWORK_KEY = _boot_secrets.get("networkKey") or ""`, module state only and never `os.environ`)
- Modify: `apps/infra/hermes/bridge/server.py` (`handle_plugin` calls `network.ensure_started(background=True)` after `_authorized`; `_save_on_sigterm` starts `network.stop` on its own thread beside `_save`)
- Modify: `apps/infra/lib/stacks/observability-stack.ts` (alarm `P35` `network-member-key-contested`, `metricName: "network.member_key_contested"`, `dimensions: { service: "hermes-bridge" }`, threshold ≥ 1 in 10 min, page), `apps/infra/test/observability-stack.test.ts`
- Test: `apps/infra/hermes/tests/test_network.py` (a fake `lightbulb` script in `tmp_path` that speaks the line protocol)

**Interfaces:**
- Consumes: the Task 2 CLI and socket; `sync._s3()`, `sync.emit_metrics`, `bridge.S3_BUCKET`, `bridge.OWNER`.
- Produces (`bridge.network`):
  - `ensure_started(*, background: bool) -> bool`: true when the sidecar runs. It is a no-op without `NETWORK_KEY` or without `<owner>/network/sessions.json`. It runs under one lock, never starts a second sidecar, and after a failure logs `network.start_failed` and waits 60 s before the next try.
  - `call(op: str, body: dict | None = None, timeout_s: float = 10.0) -> dict`: one socket line with the token. Raises `NetworkOff` when the sidecar is not running.
  - `sessions() -> list[dict]` (the sessions file as read at start), `session_for_agent(agent_id: str) -> str | None`, `agent_for_session(name: str) -> str | None`
  - `on_event: list[Callable[[dict], None]]`, filled by Task 6.
  - `stop(deadline_s: float = 6.0) -> None`
- S3 keys: `f"{OWNER}/network/state.db"`, `f"{OWNER}/network/sessions.json"`.

- [ ] **Step 1: Write the failing tests** in `test_network.py`:

```python
def test_a_pooled_microvm_without_an_invoke_starts_no_engine()      # import + /ping only -> fake binary never spawned
def test_no_key_or_no_sessions_file_starts_nothing()
def test_start_restores_state_db_and_writes_identity_key_0600()     # stat mode == 0o600; key bytes == b64decode(NETWORK_KEY)
def test_concurrent_first_invokes_start_one_sidecar()               # 8 threads ensure_started -> spawn count == 1
def test_a_failed_start_retries_after_60_s_not_before()
def test_saves_are_debounced_skipped_when_unchanged_and_conditional()  # put has IfMatch=<etag>; absent -> IfNoneMatch="*"; same sha256 -> no put
def test_a_412_stops_the_sidecar_and_keeps_the_network_off()        # metric network.state_conflict; ensure_started -> False for this process
def test_sigterm_runs_the_network_stop_beside_the_profile_saves()   # a slow profile save does not delay stop; stop waits <= 6 s
def test_four_superseded_in_ten_minutes_emit_member_key_contested()
def test_the_token_never_reaches_os_environ_or_a_file()
```

- [ ] **Step 2:** Run `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore --with pyyaml --with websockets python -m pytest tests/test_network.py -q`. Expected: FAIL (no module).
- [ ] **Step 3:** Implement `network.py` per spec §4.4 Start and Save and Stop. Spawn: `[LIGHTBULB, "member", "--home", "/opt/data/network", "--snapshot", "/opt/data/network-out/state.db", "--socket", "/opt/data/network.sock", "--name", <ws name from sessions.json "workspace">, "--sessions", <file>]`, with the token from `secrets.token_urlsafe(32)` written to stdin. A subscriber thread reads events. Save: 1 s after an event or a state-changing op, `call("snapshot", {"path": ...})`, sha256, then put.
- [ ] **Step 4:** Run the suite (the whole `tests` dir) and `cd apps/infra && npx jest test/observability-stack.test.ts`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(hermes): engine build stage and the network sidecar lifecycle`.

Skipped: an early network-only stop to save wake cost (spec §6). Add it after Task 13 measures that cost.

### Task 6: Bridge, inbound network messages as local turns, and the network RPC methods

**Files:**
- Modify: `apps/infra/hermes/bridge/network.py` (sweep, acks, held events)
- Modify: `apps/infra/hermes/bridge/turn.py:1026` (`NON_ROOM_KINDS = ("agent_mail", "paperclip_run", "network_message")`); `_resolve_session_key` (`network_message` → `f"agent:{agent_id}:lightbulb:network:{body['sessionKey']}"`, which is never per event); `_turn_user_message` (`network_message` → `message.text`, the record's `framed`); the identity line near :2040 (`"You are {name}, answering a network message from {label}. Reply with lightbulb_network if a reply is needed. {_repo_rule(agent_id)}"`)
- Modify: `apps/infra/hermes/bridge/server.py` (register `RPC_METHODS["network.invite"|"network.sessions"|"network.app"|"network.hold"|"network.wake"]`)
- Test: `apps/infra/hermes/tests/test_network_inbound.py`, `apps/infra/hermes/tests/test_network_rpc.py`

**Interfaces:**
- Consumes: `bridge.network.call`, `turn.reserve_turn`, `turn.release_turn`, `turn.register_run`, `turn.run_turn(agent_id, body, None, True)`, `turn._post_backend`.
- Produces, the ingress body (in process, never over HTTP): `{"version":1, "eventKind":"network_message", "eventId":<record id>, "targetAgentId":<agentId>, "sessionKey":<record thread or from>, "message":{"text":<framed>, "label":<label>}}`.
- Produces, RPC methods (params → payload):
  - `network.invite {for, name}` → `{line, expires_at}`, through `team.invite {mode:"person", for, name}`
  - `network.sessions {}` → `{count}`: re-read `sessions.json` from S3 and `cloud.sessions.set`
  - `network.app {op:"send"|"inbox"|"receipt"|"directory", app, person, ...}` → the engine answer (`directory` → `directory {query}`)
  - `network.hold {hold, verdict, scope?, decided_by}` → `hold.answer`
  - `network.wake {}` → `{}`: only `ensure_started` (the doorbell's whole job, spec job 3)
  - Each one runs `ensure_started(background=False)` first. When it is off, the answer is `{ok:false, code:"UNAVAILABLE"}`, the existing unavailable shape.
- Produces, the content-free hold notice: on `held`/`hold_closed` the bridge calls `hold.get` and posts `turn._post_backend(f"network/holds/{OWNER}", {"holdId", "state":"open"|"closed", "label", "target"})`. `body` and `preview` never leave the bridge.

- [ ] **Step 1: Write the failing tests:**

```python
def test_a_sweep_starts_one_turn_per_record_acked_delivered_at_start_and_read_at_end()
def test_one_sender_thread_reuses_one_hermes_session()          # two records, same thread -> same session key
def test_a_recycle_hold_leaves_the_message_unacked()            # reserve_turn False -> no ack call; next sweep runs it
def test_a_turn_that_raises_is_not_acked_read()                 # delivered yes, read no
def test_a_replayed_record_id_runs_once_per_process()           # register_run("event:<id>")
def test_a_lagged_event_triggers_a_sweep()
def test_invite_returns_the_line_and_never_logs_it()           # caplog has no ticket
def test_a_hold_notice_carries_no_text()                        # posted body keys == {holdId,state,label,target}
def test_network_rpc_when_off_answers_unavailable()
def test_network_wake_only_starts_the_sidecar()
```

- [ ] **Step 2:** Run `... python -m pytest tests/test_network_inbound.py tests/test_network_rpc.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. A sweep is `inbox.peek`. For each record not seen in this process: `reserve_turn()` (if false, stop the sweep), `register_run(f"event:{id}")`, `inbox.ack delivered`, `run_turn`, then `inbox.ack read` in `finally` only when `run_turn` returned. A sweep runs at start, on `message`, and on a lag.
- [ ] **Step 4:** Run the whole bridge suite. Expected: PASS.
- [ ] **Step 5:** Commit `feat(hermes): network messages as local turns; network.* RPC methods`.

Skipped: posting inbound messages to rooms (spec §3, by design).

### Task 7: Plugin, Friday's `lightbulb_network` tool and the run-less `eventId` fix

**Files:**
- Modify: `apps/infra/hermes/plugins/lightbulb/__init__.py:84` (`"eventId": f"{ctx.get('runId') or ctx.get('eventId') or uuid.uuid4()}:{ctx['agentId']}:tool:{next(_counter)}"`); add `NETWORK_SCHEMA` and `_network`, registered beside `lightbulb_local_sessions` at :689
- Test: `apps/infra/hermes/tests/test_plugin_network.py`, `apps/infra/hermes/tests/test_plugin_send_room_message.py` (append)

**Interfaces:**
- Consumes: `bridge.network.call` and `bridge.network.session_for_agent` (imported in process; the token stays in bridge memory).
- Produces, the tool `lightbulb_network` with `{action:"send"|"receipt"|"directory", to?, text?, reply_to?, request_id?, query?}`. Its description begins: "Messages to people's computers and their agents on the Lightbulb network (not rooms, and not your local coding sessions; for those use lightbulb_local_sessions)." `from` = `session_for_agent(ctx["agentId"])`, never a model argument. `request_id` defaults to `f"{ctx.get('runId') or ctx.get('eventId')}:net:{n}"`, with `n` counted per turn context, so a re-run turn re-sends with the same ids.

- [ ] **Step 1: Write the failing tests:**

```python
def test_send_derives_from_from_the_turns_agent_not_the_model()   # args["from"]="ceo" ignored
def test_an_agent_without_a_declared_session_is_told_so()
def test_a_rerun_turn_reuses_its_request_ids()
def test_a_run_less_room_post_uses_the_network_message_id()       # eventId startswith "<msg id>:"
def test_the_tool_answers_cleanly_when_the_network_is_off()
```

- [ ] **Step 2:** Run `... pytest tests/test_plugin_network.py tests/test_plugin_send_room_message.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the whole bridge suite. Expected: PASS.
- [ ] **Step 5:** Commit `feat(hermes): lightbulb_network tool; run-less room eventId uses the turn's event id`.

Skipped: a skill reference page. Add one line to `skills-src/lightbulb/references/` only if the model confuses the two tools in acceptance.

### Task 8: Backend, the member key, `sessions.json`, the connect, invite and reconnect routes, and the teardown delete

**Files:**
- Modify: `apps/backend/core/services/agentcore_runtime.py:159-191` (`write_boot_secret(owner_id, *, hook_token, gateway_token, bedrock_bearer, drop_network_key: bool = False) -> bool`: carry `prior["networkKey"]` over, and mint `base64.b64encode(os.urandom(32)).decode()` when it is absent. A new key counts as a boot change. `drop_network_key=True` writes the secret without it.)
- Modify: `apps/backend/core/services/hermes_provisioning.py:653` (`prepare_hermes_workspace` writes `sessions.json` through `network_member.write_sessions` when it is absent, so every workspace gets a member at provision and at reconcile; decided 2026-10-24. `roll_hermes_runtimes.py` already runs `prepare_hermes_workspace` and restarts an idle session on a boot change, so Task 13's roll mints the existing fleet); `:473-500` (extract `_delete_prefixes(prefixes: list[str])`; `teardown_hermes_runtime` step 4 also deletes `f"{owner_id}/network/"`, whatever `delete_profiles` is); `write_profiles` ends with `await network_member.publish_sessions(owner_id)`
- Modify: `apps/backend/core/services/runtime_rpc.py:190` (`_invalidate`: for `agents.*` methods, also `publish_sessions`)
- Create: `apps/backend/core/services/network_member.py`
- Create: `apps/backend/routers/network.py`; Modify: `apps/backend/main.py` (`include_router(..., prefix="/api/v1/settings/network")`)
- Test: `apps/backend/tests/unit/services/test_network_member.py`, `apps/backend/tests/unit/services/test_agentcore_runtime.py` (append), `apps/backend/tests/unit/services/test_hermes_provisioning_teardown.py` (append), `apps/backend/tests/unit/services/test_hermes_provisioning.py` (append), `apps/backend/tests/unit/routers/test_network_routes.py`

**Interfaces:**
- Produces (`network_member`):
  - `sessions_key(owner_id) -> str` = `f"{owner_id}/network/sessions.json"`
  - `async def write_sessions(owner_id: str, *, apps: list[dict] | None = None) -> dict`. It writes `{"workspace": <org name>, "sessions": [{"name": <slug of agent name>, "agent": "hermes", "title": <agent name>, "agentId": <id>} for each agent in messaging.load_agent_roster] + apps}`, keeping the earlier app entries when `apps` is None. More than 100 entries raises `ApiProblem("INVALID_REQUEST", "This workspace has reached 100 network addresses.")`.
  - `async def publish_sessions(owner_id) -> None`: when `sessions.json` exists, rewrite it and call `container_rpc.call(owner_id, "network.sessions", {}, timeout_s=10)`. It is best effort and never raises.
  - `async def connect(owner_id, user_id, name) -> dict`: `container_rpc.call(owner_id, "network.invite", {"for": user_id, "name": name})`. When `sessions.json` is absent (a workspace not yet rolled, or one just reconnected), it first runs `prepare_hermes_workspace(owner_id)` and a `stop_runtime_session` when the boot secret changed.
  - `async def reconnect(owner_id) -> None`: `write_boot_secret(..., drop_network_key=True)` FIRST (so a cold start in the gap reads no key), then `stop_runtime_session`, then delete `f"{owner_id}/network/"` (ruled 2026-10-24; stop-first left a window for an old-key session to save after the delete). `connect` deletes `state.db` once more before minting; the bridge skips its save when `sessions.json` is gone. The next `connect` mints a new key, which makes a new team.
  - `app_person(user_id) -> str` = `hashlib.sha256(user_id.encode()).hexdigest()[:8]`
- Produces, routes (Clerk, `resolve_owner_id`): `GET ""` → `{connected: bool}` (`sessions.json` exists). `POST "/invite"` → `{line, expiresAt}`, with `Cache-Control: no-store`; a parked runtime outlasts API Gateway's 30 s and the client retries on 504 or `RUNTIME_UNAVAILABLE`. `POST "/reconnect"` (org admin only) → `{}`.
- The invite line is never logged or stored.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_write_boot_secret_carries_network_key_across_a_reconcile()     # prior has it -> same value
async def test_write_boot_secret_mints_once_and_reports_a_boot_change()
async def test_a_fresh_provision_mints_the_network_key_and_sessions_json()
async def test_no_other_module_reads_network_key()          # grep apps/backend for "networkKey": only agentcore_runtime.py
async def test_teardown_deletes_network_state_even_without_delete_profiles()
async def test_sessions_json_lists_every_agent_and_keeps_app_entries()
async def test_a_101st_session_is_refused_with_a_clear_error()
async def test_agents_create_republishes_sessions()
async def test_invite_route_returns_the_line_once_and_stores_nothing()        # no put_item; no log record contains the line
async def test_reconnect_is_admin_only_and_drops_key_and_state()
```

- [ ] **Step 2:** Run `cd apps/backend && uv run pytest tests/unit/services/test_network_member.py tests/unit/services/test_agentcore_runtime.py tests/unit/services/test_hermes_provisioning_teardown.py tests/unit/services/test_hermes_provisioning.py tests/unit/routers/test_network_routes.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command. Expected: PASS.
- [ ] **Step 5:** Commit `feat(backend): the workspace member key, sessions.json, and the connect routes`.

Skipped: a "Reconnect workspace" prompt driven by `KEY_MISMATCH`. The panel shows the button; add detection when the bridge reports it.

### Task 9: Backend, member holds as Needs-you items (web unchanged)

**Files:**
- Modify: `apps/backend/routers/network.py` (internal route `POST /api/v1/internal/messaging/network/holds/{owner_id}`, hook token through `routers/messaging._require_hook_token`; mounted on a second router with the internal prefix)
- Modify: `apps/backend/core/services/network_member.py` (`async def hold_notice(owner_id, notice: dict) -> None`, `async def decide_hold(auth, hold_id, action_id) -> None`)
- Modify: `apps/backend/core/services/activity.py` (`render_network_hold(row, *, seen)` next to `render_webhook_agent_ask`, reusing `kind="session"`, `subtype="join_request"`, `WEBHOOK_ASK_REASON`, and the actions Send / Decline / Always allow with `preview=None`; `_render_network_hold_rows` in the gather at :853; `run_action` branch `parts[0] == "nethold" and len(parts) == 2`)
- Test: `apps/backend/tests/unit/services/test_network_holds.py`, `apps/frontend/src/components/activity/__tests__/ForYou.test.tsx` (append one case)

**Interfaces:**
- Consumes: Task 6's notice `{holdId, state, label, target}`; `website_logins._humans_to_ask(owner_id, None)`; `messaging_repo.put_activity_row`; `webhook_agents._invalidate_activity`.
- Produces: an Activity row `{"kind": "network_hold", "holdId", "label", "target", "createdAt", "expiresAt"}`, item id `f"nethold:{holdId}"`. Who sees it: for an app session target (`{app}-{person}`), the person whose `app_person` matches; for any other target, `_humans_to_ask(owner_id, None)`. `state:"closed"` resolves the item. An action maps `send` → `allow`, `decline` → `decline`, `allow` → `allow` with `scope:"always"`, and then `container_rpc.call(owner, "network.hold", {hold, verdict, scope, decided_by: auth.user_id})`. A viewer without the row gets `ACTIVITY_NOT_FOUND`.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_a_notice_files_one_item_per_asked_human_with_no_text()   # summary.preview is None
async def test_an_app_session_hold_goes_to_that_person_only()
async def test_a_tap_answers_the_hold_with_decided_by_the_tapper()
async def test_a_closed_notice_resolves_the_item()
async def test_another_viewer_cannot_answer()
async def test_the_internal_route_needs_the_hook_token()
```

```ts
it("renders a nethold item with Send, Decline and Always allow", ...)  // generic renderer; no new web code
```

- [ ] **Step 2:** Run `cd apps/backend && uv run pytest tests/unit/services/test_network_holds.py -v` and `cd apps/frontend && pnpm vitest run src/components/activity/__tests__/ForYou.test.tsx`. Expected: backend FAIL, vitest PASS (that proves the reuse).
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run both. Expected: PASS.
- [ ] **Step 5:** Commit `feat(backend): member holds as Needs-you items`.

Skipped: a push for a member hold. Add `send_push_to_user` (as `_announce_held` does) if acceptance shows the item goes unseen.

### Task 10: Backend, hosted MCP tools send through the member

**Files:**
- Modify: `apps/backend/core/services/hosted_mcp.py` (`send_message` gains `network_to: str | None`, one more of the exactly-one targets; `message_receipt` handles the pinned destination `network:<to>`; new read tool `network(action: Literal["inbox","directory"], query: str = "")`)
- Modify: `apps/backend/core/services/network_member.py` (`async def app_call(auth, slug, client, op, **fields) -> dict`)
- Test: `apps/backend/tests/unit/services/test_hosted_mcp_network.py`

**Interfaces:**
- Consumes: `_pin_destination`, `_sender(get_access_token())` → `(slug, client)`, `write_sessions(apps=...)`, `publish_sessions`, `app_person`.
- Produces: `app_call` makes sure the session `{"name": f"{slug}-{app_person(user)}", "agent": slug, "title": f"{client} via {first name}", "app": slug, "person": app_person(user)}` is in `sessions.json`. When it is new, it publishes first. Then it calls `container_rpc.call(owner, "network.app", {"op", "app": slug, "person": app_person(user), ...})`. `send_message(network_to=...)` pins `network:<to>` and passes `request_id=str(request_id)`. A runtime that is starting surfaces as `ToolError("RUNTIME_UNAVAILABLE: the workspace is starting; retry with the same request_id")`.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_a_network_send_declares_the_app_session_then_sends()
async def test_a_retry_with_the_same_request_id_sends_once()      # first call times out; retry -> engine sees one request_id; receipt same id
async def test_network_to_is_exclusive_with_the_other_targets()
async def test_the_receipt_reads_back_through_the_member()
async def test_inbox_and_directory_return_engine_records_and_store_nothing()
```

- [ ] **Step 2:** Run `cd apps/backend && uv run pytest tests/unit/services/test_hosted_mcp_network.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Engine `request_id` dedupe on the app session makes a retry safe.
- [ ] **Step 4:** Run it and `tests/unit/services/test_hosted_mcp_*.py`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(backend): hosted assistants send through the workspace member`.

Skipped: the deferred webhook agents on the network (spec T8).

### Task 11: Web, the Settings "Connect your computer" section

**Files:**
- Create: `apps/frontend/src/components/control/panels/NetworkSection.tsx` (same pattern as `AgentKeysSection.tsx`: `useApi`, SWR on `/settings/network`, line shown once in `fresh` state)
- Modify: `apps/frontend/src/components/control/panels/ConnectionsPanel.tsx:71` (mount `<NetworkSection />` above `<AgentKeysSection />`)
- Test: `apps/frontend/src/components/control/panels/__tests__/NetworkSection.test.tsx`

**Interfaces:**
- Consumes: Task 8 routes.
- Produces, exact copy: heading "Connect your computer". Step 1: "Install Lightbulb on your Mac or Linux computer:" + `curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh`. Step 2, button "Get my line", then "Paste this in Terminal. It works once, for 24 hours." + the line + Copy. While retrying: "Starting your workspace… this can take a minute." Admin-only link: "Reconnect workspace" (confirm text: "Every computer will need a new line.").

- [ ] **Step 1: Write the failing tests:**

```ts
it("shows the installer and gets a line once")            // line visible after click; gone after remount
it("retries on 504 until the line arrives")               // fake api: 504, 504, 200
it("hides Reconnect workspace from members")
it("never says mail")                                      // container.textContent not /mail/i
```

- [ ] **Step 2:** Run `cd apps/frontend && pnpm vitest run src/components/control/panels/__tests__/NetworkSection.test.tsx`. Expected: FAIL.
- [ ] **Step 3:** Implement. Retry every 10 s, at most 12 times.
- [ ] **Step 4:** Run it plus `ConnectionsPanel.test.tsx`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(web): Connect your computer`.

Skipped: a mobile copy of the panel (a Mac owner pastes in a terminal). Add it if people ask from the phone.

### Task 12: Backend, the doorbell route and the `network_wake` work kind (decided 2026-10-24)

**Files:**
- Modify: `apps/backend/core/repositories/messaging_repo.py` (docstring rows `OWNER#<owner> NETWAKE` and `OWNER#<owner> WORK#NETWAKE#<ulid>`; `def netwake_sk() -> str`, `def work_netwake_sk(ulid: str) -> str`)
- Modify: `apps/backend/core/services/network_member.py` (`write_sessions` adds `"wake": {"url": f"{settings.PUBLIC_API_BASE}/api/v1/network/wake/{token}", "token": token}` with `token = f"{owner_id}.{secrets.token_urlsafe(32)}"`, minted once, with `tokenHash` (sha256) kept on the `NETWAKE` row. `reconnect` deletes that row.)
- Modify: `apps/backend/routers/network.py` (public `POST /api/v1/network/wake/{token}`, no auth beyond the token), `apps/backend/main.py` (mount it)
- Modify: `apps/backend/core/services/openclaw_work.py:104-155` (`deserialize_pointer`: a `network_wake` branch whose keys are exactly `{kind, ownerId}`); `_process_message` (`network_wake` → `container_rpc.call(owner_id, "network.wake", {})`)
- Test: `apps/backend/tests/unit/services/test_openclaw_work_pointer.py` (append), `apps/backend/tests/unit/routers/test_network_wake.py`

**Interfaces:**
- Consumes: Task 4's E4 (a computer runs the system `curl` on the founder's `wake`), Task 6's `network.wake` RPC.
- Produces, the wake route: a malformed token, an unknown owner or a hash mismatch → 404 with an empty body and no write. A good token → a conditional update `lastWakeAt < now - 10`. When it wins, the route writes `{pk: OWNER#<owner>, sk: WORK#NETWAKE#<ulid>, payload: {kind: "network_wake", ownerId}, messageGroupId: f"netwake#{owner}", messageDeduplicationId: <ulid>, expiresAt: now + 7 d}` and answers 202. When it loses, it answers 202 and writes nothing. The Pipe already forwards every `WORK#` insert, and the projector ignores it.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_a_bad_token_is_404_and_writes_nothing()
async def test_one_wake_per_workspace_per_10_s()            # 50 calls in 1 s -> one WORK#NETWAKE# row
async def test_the_token_never_appears_in_logs()
def test_network_wake_pointer_is_exact()                    # extra key -> ValueError
async def test_a_network_wake_invokes_network_wake_rpc()
async def test_reconnect_deletes_the_netwake_row()
```

- [ ] **Step 2:** Run `cd apps/backend && uv run pytest tests/unit/routers/test_network_wake.py tests/unit/services/test_openclaw_work_pointer.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the same command plus `tests/unit/services/test_openclaw_work_*.py`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(backend): network doorbell route and the network_wake work kind`.

Skipped: a signature, an `at` window and a per-sender limit (spec §4.6, by ruling).

### Task 13: Pin, roll, and acceptance

**Files:**
- Modify: `hermes-version.json` (`dev.tag`, then `prod.tag`, to the `h-<date>-<sha7>` candidate that `build-hermes-image.yml` pushed after Tasks 5-7 merged)
- Create: `docs/superpowers/plans/2026-10-24-lightbulb-network-plan-c-phase-1-acceptance.md` (the evidence file)

**The two computers:** the owner's Mac, and the owner's WSL instance on the Windows server (reachable over Tailscale). WSL is installed with `install.sh` (the `lightbulb-linux-x86_64` asset), runs `lightbulb daemon`, and joins the owner's workspace with its own Settings line. It is the permanent second computer. The engine's existing `two-mac-test.yml` (GitHub `macos-latest`) stays the free cross-network check: run it once with `gh workflow run two-mac-test.yml --repo TheLightbulbCompany/cli -f target=<member short>/friday`.

- [ ] **Step 1:** Land the `dev.tag` flip PR. Watch the deploy with `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60`. Then, in the dev backend task, run `uv run python -m scripts.roll_hermes_runtimes --all`. This also mints every workspace's member (Task 8). Verify with `aws bedrock-agentcore-control get-agent-runtime` that `containerUri` ends with the new tag. Expected: exit 0.
- [ ] **Step 2:** Run the demo on dev. Record each item in the evidence file with its timestamp, its receipt states, and a screenshot or log line:
  1. Settings → "Get my line" → paste on the Mac, then on WSL with a second line → `lightbulb team` on both lists the workspace. Friday's first message on the Mac is held once in the dialog, then "always". On WSL it is answered with `lightbulb approve`.
  2. Wait until the runtime parks (> 30 min idle). Send to `friday` from WSL. Expected: the doorbell rings, then `queued` → `delivered` in about a minute, and a reply arrives. Record the seconds from send to `delivered`.
  3. Stop the WSL daemon, have Friday send to it, start the daemon again. Expected: `queued` → `stored`.
  4. With the Mac off, ChatGPT through the hosted MCP sends to WSL. It arrives labelled "ChatGPT via <Person>". Retry once with the same `request_id`: one message.
  5. A stranger computer's first message to Friday (the `two-mac-test.yml` runner, joined by an `ask` link) shows as a Needs-you item on the phone. Tap Send. The hold closes, and its record says `decided_by` = the tapper.
  6. Set `LIGHTBULB_RECYCLE_AFTER_S=600` on a dev runtime with `--respec`. The recycle keeps `state.db` (the roster and receipts survive) and loses no message.
  7. Run a second engine with the member key on WSL (outside AWS). `network.member_key_contested` fires, and P35 pages.
  8. Wake cost: count network-only wakes for 24 h and multiply by the 30 min idle, using AgentCore's billed session-hours from Cost Explorer. Write the dollar figure per workspace per month.
- [ ] **Step 3:** If all 8 pass, land the `prod.tag` flip, dispatch `gh workflow run deploy.yml --ref main` when no deploy run is in flight, and run `roll_hermes_runtimes --all` on prod. Repeat items 1 and 2 on the owner's prod workspace with the Mac and WSL.
- [ ] **Step 4:** Commit the evidence file with `docs: network phase 1 acceptance`.

Skipped: delete the spec and this plan after prod acceptance, per the repo's planning-docs rule. The evidence file is the record. Phase 2 (session streams to the web and phone) starts next.
