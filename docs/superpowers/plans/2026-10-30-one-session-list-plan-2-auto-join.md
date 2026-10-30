# One session list, Step 2: sign in to a workspace, join its network — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person signed in to a workspace in the desktop app ends up with their computer on the workspace's network and their engine signed in, without an invite, and no script on the hosted page can make that happen or undo it.

**Architecture:** Three authenticated parties: the engine (mints as the cloud member; joins on the Mac), the backend (hands the line to a paired Mac or a signed-in computer, never to a Clerk token), native Rust (`network_connect.rs`: the only code that fetches a line, joins, leaves or signs the engine in). The page reads a status and asks for a native dialog; it never sees a ticket.

**Tech Stack:** Rust (engine; Tauri), Python/FastAPI (backend, bridge), React/TS (footer).

**Spec:** `docs/superpowers/specs/2026-10-30-one-session-list-design.md` §4.5, §6 A8-A9, §8 T1 T2, §10. Research: `docs/research/2026-10-30-sessions-autojoin-and-presence.md`. Founder's security ruling overrides §4.5 where they differ. Code wins over docs.

Worktrees: engine `~/Desktop/lightbulb-network-wt/instance-invite` (branch `feat/instance-invite`), monorepo `~/Desktop/lightbulb.nosync/.claude/worktrees/step2-auto-join` (branch `feat/sessions-step2-auto-join`).

## Global Constraints

- The hosted page must never be able to join or leave a network. `engine_ask` stays read-only (`directory`, `sessions`, `team.get`); do not widen it.
- The page never receives, holds or passes a ticket. `/network/join-line` refuses Clerk tokens.
- The join ticket is a bearer credential: never logged, `Cache-Control: no-store`, never in a URL, not in an error message.
- T1: join silently when the computer is alone in its own network, with a notice once after; ask only when it is on another multi-member network. T2: the workspace's cloud member anchors the network and mints once links.
- Notice text, exact: `Your computer joined <Workspace>'s network. Teammates on it see the names, folders and status of your sessions, never your files or conversations. Hide a folder: lightbulb folder <path> off.`
- Connecting text, exact: `Connecting your computer to <Workspace>…`
- Ask text, exact: `Your computer is on <Team>. Join <Workspace>'s network instead?` with buttons Join / Not now.
- Free users (owner id equals user id) see nothing and nothing runs.
- Cold start is about 55 s inside a 90 s limit: everything runs in the background.
- The engine reads debug-only env only under `cfg!(debug_assertions)` (`cli.rs:63` `test_env`).
- Step 3 owns `local_sessions/*`, `networkSessionId`, `useSessionRows`' data and the "background service is off" fault; Step 4 owns sidebar layout. Keep footer changes one line in `WorkspaceSidebar.tsx`.
- Do not change `apps/infra/hermes/Dockerfile`'s pin. Never `git stash`. No full test suites locally: focused tests only. Keep Rust `target` under 6 GB (`du -sh target`; `cargo clean -p` or remove it between runs). Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Contract between tasks

Bridge RPC (Task 2): `network.invite {}` → `{ticket, team_id, expires_at}`; `network.team {}` → `{team_id, member_count}`.

Backend (Task 3), all under `/api/v1/network`, all `no_store`. Auth `desktop_or_computer`: a paired-device EdDSA token (`Authorization: Bearer`, via `machine_auth.verify_upgrade(token, "openclaw-macos")`, as `routers/messaging.py:101-109`) or, when `X-Lightbulb-Proof` is present, `network_login.computer_identity`. Yields `(user_id, owner_id)`; a Clerk JWT gets 403.
- `GET /team` → `{workspace: str|null, teamId: str|null, userId: str, name: str|null}`. `workspace: null` is a person with no workspace (owner == user). No RPC, no wake. 403 if the user is not in the workspace's people (`website_logins._humans_to_ask`).
- `POST /join-line` → `{ticket, teamId, workspace, name, expiresAt}`. 409 `NO_WORKSPACE` for a free person; 409 "no workspace agents yet" when `sessions_exist` is false; 503 + `Retry-After: 10` on `UNAVAILABLE`/`RUNTIME_UNAVAILABLE`.
- `POST /device/approve-desktop {userCode}` → `{status}`. Paired-device token only (no proof path). Purpose must be `login`, else 403. Calls `network_login.approve` with `AuthContext(user_id, org_id or None)`.
`name` is the person's first name, as `network_login._name`.

Engine (Task 1):
- Instance `team.invite` → the usual answer, with `wake` signed into the ticket when the member has one.
- `lightbulb login --app` (hidden from `--help`): opens nothing, asks nothing, joins nothing; prints exactly one line `{"user_code":"<code>"}` to stdout before polling, then exits 0 once the proof is saved.
- `lightbulb login` (no flag): after the proof, joins the workspace's network (spec §4.5 CLI flow).

Native commands (Task 4), taking no ticket or team: `network_status() -> Status`, `network_connect()`, `network_leave()`, `network_notice_seen()`. All camelCase JSON:
`Status = {state: "hidden"|"off"|"connected"|"connecting"|"optedOut"|"failed", workspace: string|null, notice: string|null}`; `notice` is the workspace name while the once-notice is pending.

## Review Focus

1. Page script calls `network_connect`/`network_leave`: nothing changes until a person clicks a native alert. Task 4 test on the decision path; Task 4 by construction (alert is in the command).
2. A once link is spent or stale while the join is retried, or `name_taken` on a person's second Mac: retries with `<Name> (<computer>)` on the same line, then fails visibly and tries a fresh line next tick. Task 4 test.
3. A person ran `lightbulb leave` while the app was closed: next launch must not re-join. Task 4 test `person_left_is_an_opt_out`.
4. The member moved networks (`invite --agents`, `network.leave`) after a `teamId` was stored: stored id cleared, not stale. Task 3 test.
5. `write_sessions`/`_write` rewrites `sessions.json`: `teamId` survives like `joined` and `wake`. Task 3 test.

---

### Task 1: Engine — instance invites, `login --app`, CLI join (engine repo, one PR)

**Files:**
- Modify: `src/daemon/net_ops.rs:98-120` (instance forced `Once`, drop UNSUPPORTED, skip `not_a_person` for instance), `src/daemon/ipc.rs:177` comment (`NOT_ON_AN_INSTANCE`'s doc)
- Modify: `src/ticket.rs` (`mint`, `sign` take `wake: Option<Wake>`, set before signing; `ticket.rs:106`), callers `net_ops.rs:615,666`, `tests/join.rs:149,202`, unit tests. Wake comes from `daemon.cloud`'s `Cloud.wake`.
- Modify: `src/main.rs:196,393`, `src/login.rs` (`--app`; `NO_BROWSER` via `cli::test_env`; after-proof join; drop the "once an admin adds them" line), `src/cli.rs` if `test_env` needs `pub(crate)`
- Modify: `docs/usage.md`, `Cargo.toml`/`Cargo.lock` version per how #108 did it
- Test: `tests/member.rs` or `tests/embed.rs`, `tests/login.rs`

**Interfaces:** Produces the Engine block of the Contract. CLI join uses the workspace-tools signed-call helper already in the engine (`X-Lightbulb-Proof`/`X-Lightbulb-Sig`: find it with `grep -rn X-Lightbulb-Proof src`) to call backend `GET /team` and `POST /join-line`, then IPC `team.get`/`team.join`.

- [ ] **Step 1: Failing tests:** `instance_invite_is_once_and_carries_the_wake` (embed instance with a doorbell: `team.invite {mode: "reusable"}` answers a ticket whose decode has `wake == Some(doorbell)`, `signed_by_inviter` true, and whose roster link is `once`); `instance_invite_without_a_doorbell_has_no_wake`; a person's socket ticket still has `wake: None`; `a joiner_is_admitted_on_an_instance_once_link` in-process if `tests/embed.rs` has two-instance helpers; `login_app_prints_one_json_line_and_opens_nothing` against the mock backend in `tests/login.rs`.
- [ ] **Step 2: Run them, expect FAIL.** `cargo test --test <file> <name>`.
- [ ] **Step 3: Implement.** `--app` skips `open`, `println!`s only the JSON line (no "Confirm in your browser"), skips `ask_upload_mode` and the join. Plain `login`, after saving: `GET /team`; `workspace` null → nothing more; `teamId` == `team.get.team_id` → "On <Workspace>'s network."; alone (`team.get.members.len() == 1`) → `POST /join-line`, `team.join {ticket, name}`, suffix `<Name> (<computer>)` on `name_taken`, print the notice text; not alone → print `You are on <Team>. To join <Workspace>'s network: lightbulb leave, then lightbulb login.` A failure in this block prints one line and `login` still exits 0: the proof is saved.
- [ ] **Step 4: Tests pass.**
- [ ] **Step 5: Real two-node run in OrbStack**, never a second daemon on this Mac: per the memory note, build with `docker run rust:1-bookworm cargo build --release --target-dir /out` (volumes `lb-e2e-target`, `lb-e2e-cargo`), two `debian` containers on one docker network. Container A runs `lightbulb member` with a sessions file carrying `wake`; mint over its token socket `{op: "team.invite", mode: "once", token, id}`. Confirm the decoded ticket carries the `wake`. Container B runs `daemon`, then `docker exec -t` `lightbulb join '<ticket>' --name Leonardo`. Expect admitted, and `lightbulb team` on B lists both. Record the output in the report. Remove the containers after.
- [ ] **Step 6: Commit.**

### Task 2: Bridge RPCs

**Files:** Modify `apps/infra/hermes/bridge/server.py:522` region; Test `apps/infra/hermes/tests/` (bridge test file already covering `_network_join`: `grep -ln _network_join apps/infra/hermes/tests`).

**Interfaces:** Produces the Bridge RPC block.

- [ ] **Step 1: Failing tests:** `network_invite_returns_ticket_and_team_id` (asks engine `team.invite {mode: "once", code: false}` and `team.get`), `network_invite_refused_is_an_op_error`, `network_team_counts_members`, `network_invite_logs_no_ticket` (caplog holds no ticket).
- [ ] **Step 2: Run, FAIL.**
- [ ] **Step 3: Implement** `_network_invite`, `_network_team` through `_net(..., run=...)`, register in `RPC_METHODS`. Fix the stale "waits for that admin's Allow" docstring in `_network_join` while there. `ask` returns the `result` body.
- [ ] **Step 4: Pass. Step 5: Commit.**

### Task 3: Backend — stored `teamId`, routes, backfill

**Files:**
- Modify: `apps/backend/core/services/network_member.py` (`_write` preserves `teamId`; `team_info(owner)`, `mint_line(owner)`; `join_network`/`leave_network` clear the stored `teamId`; `NETWORK_ALREADY_JOINED` text; module docstring's "A workspace never invites"), `apps/backend/routers/network.py`, `apps/backend/core/services/network_login.py` only if `_name` must be public
- Create: `apps/backend/scripts/backfill_network_team_ids.py`
- Test: `apps/backend/tests/unit/services/test_network_member.py`, `apps/backend/tests/unit/routers/test_network_login.py` (or new `test_network_join_line.py`)

**Interfaces:** Consumes Bridge RPC. Produces Backend block. `async def stored_team_id(owner_id) -> str|None`; `async def mint_line(owner_id) -> dict` (`{ticket, teamId, expiresAt}`, stores `teamId`).

- [ ] **Step 1: Failing tests:** `team_id_survives_write`; `join_network_clears_team_id`; `leave_network_clears_team_id`; `mint_line_stores_team_id`; `mint_line_unavailable_is_503`; route tests: `join_line_refuses_a_clerk_token` (403), `join_line_device_token_returns_ticket_no_store`, `join_line_not_a_member_403`, `join_line_free_409`, `join_line_no_sessions_409`, `team_makes_no_rpc` (container_rpc mock not called), `team_free_workspace_null`, `approve_desktop_refuses_agents_purpose`, `approve_desktop_mints_the_login_proof`. `already_joined_message` asserts `Your workspace's agents are already on a network. Sign in with lightbulb login to join it.`
- [ ] **Step 2: Run, FAIL.**
- [ ] **Step 3: Implement.** RPC `timeoutMs` 85000 (under the 90 s ingress). Backfill: walks owners with a `sessions.json` holding `joined: true` and no `teamId` (`--owner-id` or `--all`, `--dry-run`, same style as `scripts/roll_hermes_runtimes.py`), calls `network.team`, stores. A docstring states it runs after the image with `network.team` is rolled. The route logs neither ticket nor body.
- [ ] **Step 4: Pass, `ruff` on touched files. Step 5: Commit.**

### Task 4: Desktop native — `network_connect.rs`

**Files:**
- Create: `apps/desktop/src-tauri/src/network_connect.rs`
- Modify: `apps/desktop/src-tauri/src/engine_client.rs` (`pub async fn ask_for(op, body, limit)`; remove `#[allow(dead_code)]` if now used), `apps/desktop/src-tauri/src/lib.rs` (module, four commands in `generate_handler`, `network_connect::kick(&app)` in `send_auth_token` and after the persisted start at launch)

**Interfaces:** Consumes Backend block, Engine block, `engine_client`, `pair_state::load`, `keychain::load(KeychainSlot::Cli, KEYCHAIN_SERVICE)`, `device_token::mint_device_token(pem, device_id_cli, owner_id, 90)`, `LIGHTBULB_API_BASE`, tauri dialog plugin as in `desktop_permissions_set`, `notify.rs`. Produces the Native block.

Pure core:
```rust
enum Action { Done, Join, Ask, OptOut, Stay }
fn decide(engine_team: &str, alone: bool, removed: bool, ws_team: Option<&str>, opted_out: bool, joined_before: Option<&str>) -> Action
```
Order: `ws_team == engine_team` → Done; `opted_out` → Stay; `removed` → OptOut; `alone && joined_before.is_some_and(|t| t != engine_team)` → OptOut (the person left); `alone` → Join; otherwise Ask. `ws_team` None and not opted out: fetch the line first, then decide with its `teamId` (one spare once link at most, since it stores the id).

State file `<app data>/network_connect.json` (0600): `{optOut: {owner: true}, joined: {owner: teamId}, notice: string|null}`.

Run (`kick`, single-flight, at most once per 10 min, 60 s after a failure, immediately when the pair's owner changed):
1. `pair_state::load` none → hidden. `team.get` Err → off. `GET /team`: `workspace` null → hidden.
2. Done/Join: if `login.json`'s proof is missing, expired, or `sub != userId`: spawn `<state>/bin/lightbulb login --app`, read `{"user_code"}` from stdout, `POST /device/approve-desktop`, wait ≤ 60 s. Failure is logged and does not stop the join.
3. Join: state connecting; `POST /join-line` with a 100 s timeout, retry up to 3 on 503/5xx/timeout honouring `Retry-After`; `ask_for("team.join", {ticket, name}, 200 s)`; on `name_taken` the same ticket with `<Name> (<computer>)`; on `admitted` save `joined`, `notice`, post a native notification `Your computer joined <Workspace>'s network.` On anything else: failed, ticket dropped, next tick tries afresh.
4. Ask: native alert with the Ask text, once per run. Join → `team.leave`, then step 3. Not now → OptOut.

Commands: `network_connect`: native alert `Connect this computer to <Workspace>'s network?`; on yes clear the opt-out and kick now. `network_leave`: native alert `Leave <Team>?`; on yes `team.leave`, OptOut, clear `notice`. `network_notice_seen`: clear `notice`. `network_status`: last known Status.

- [ ] **Step 1: Failing tests** in the module: `same_team_is_done`, `alone_joins`, `opted_out_stays`, `removed_opts_out`, `person_left_is_an_opt_out`, `other_network_asks`, `same_team_wins_over_opted_out`; `join_retries_name_taken_with_the_computer` (stub socket as `engine_client_tests::stub`), `join_failure_keeps_no_ticket_in_status` (serialized Status and error hold no ticket substring), `proof_needs_login` (missing, expired, other sub), `opt_out_survives_reload`.
- [ ] **Step 2: Run** `cargo test -p <desktop crate> network_connect`, FAIL.
- [ ] **Step 3: Implement.** No `log` line holds a ticket, a JWT or the device token.
- [ ] **Step 4: Pass; `cargo check`. Step 5: Commit.**

### Task 5: Frontend footer

**Files:**
- Create: `apps/frontend/src/components/desktop/NetworkConnect.tsx`, `apps/frontend/src/components/desktop/__tests__/NetworkConnect.test.tsx`
- Modify: `apps/frontend/src/components/workspace/WorkspaceSidebar.tsx:116` (one element beside `NodeStatusPill`)

**Interfaces:** Consumes Native block through `window.__TAURI__.core.invoke` (`bridge()` in `components/messaging/localTerminal.ts`; export it if needed).

- [ ] **Step 1: Failing tests:** renders nothing in a browser, on an older app (`command not found`), and for `hidden`, `off`, `connected`; `connecting` shows the Connecting text; `optedOut`/`failed` shows a Connect button that invokes `network_connect` with no args; a `notice` shows the Notice text and Leave invokes `network_leave`, dismiss invokes `network_notice_seen`; no invoke is passed a `ticket`.
- [ ] **Step 2: Run** `npx vitest run src/components/desktop/__tests__/NetworkConnect.test.tsx`, FAIL.
- [ ] **Step 3: Implement.** Poll `network_status` every 3 s while connecting, else every 60 s. Reuse the footer's existing pill styles (read `NodeStatusPill`).
- [ ] **Step 4: Pass; `tsc` on the package. Step 5: Commit.**

### Task 6: Finish

- [ ] Final whole-branch reviews, both repos. Security focus: no path from page script to join/leave, no ticket in logs.
- [ ] Real verification where possible; record what could only be tested.
- [ ] Push both branches; two PRs (`pr` skill), the monorepo one naming its dependencies: engine release, Hermes image with the new bridge, backfill after the roll, desktop release bundling the engine.
