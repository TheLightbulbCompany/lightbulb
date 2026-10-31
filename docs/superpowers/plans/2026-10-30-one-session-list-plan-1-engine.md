# One session list, plan 1: the engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `lightbulb` engine lists every session Claude itself lists, with Claude's own busy / waiting / idle words, keeps ended sessions for seven days with an owner-only resume command, shows offline and parked computers as such, and no longer carries the trust leftovers or lets a test run replace the real background service.

**Architecture:** Claude discovery moves from the undocumented pid files to `claude agents --json --all` (documented), cached in the daemon and refreshed on hook events. Every session seen gets a store row; a row whose process or listing entry is gone is ended with a reason and a title, and published to peers in a new optional `ended` list on the presence frame. The engine's hooks become the only status writer, gaining `Notification` and `PermissionRequest`. The `directory` op, `find_sessions` and `who` show the richer records.

**Tech Stack:** Rust 2021 (rust-version 1.91), tokio, rusqlite (migration V16), serde, iroh (unchanged). Repo: `~/Desktop/lightbulb-network.nosync` (TheLightbulbCompany/cli). Build a fresh worktree from `origin/main` for this plan (`git worktree add ~/Desktop/lightbulb-network-wt/one-list -b feat/one-session-list origin/main`), never on another agent's branch, never `git stash`.

**Spec:** `docs/superpowers/specs/2026-10-30-one-session-list-design.md` (monorepo, branch `docs/one-session-list`), §4.1, §4.2, §4.6, and the research under `docs/research/2026-10-30-sessions-*.md`. This plan implements Step 1 only.

## Global Constraints

- Wire: an extra optional field on `Remote` or `Presence` is safe (`#[serde(default, skip_serializing_if = …)]`); a new `status` or `tier` value is not (old peers drop the whole presence frame, `frames.rs:302-317`); no new `Frame` variant; `PROTOCOL` stays 1. A `waiting` session travels as `status: "busy"` plus `waiting_for`.
- Retention: `WEEK = 7 * DAY` for ended rows and for an offline member's presence. `kept` and `known` stay on `DAY` (mail routing for old peers). Ended list on the wire: at most 50, newest first.
- Claude surfaces, from `docs/research/2026-10-30-sessions-harness-surfaces.md` §1 (all confirmed): `claude agents --json --all` rows carry `sessionId`, `cwd`, `kind` (`interactive | background`), `startedAt`, and while the process lives `pid` and `status` (`busy | waiting | idle`), `waitingFor` when waiting, and for background rows `id` and `state` (`working | blocked | done | failed | stopped`). Claude maps its own `shell` state to `busy`. Rows with `parkedJobId` are left out by Claude. `claude --resume <sessionId>` resumes any of them.
- Claude hooks, from code.claude.com/docs/en/hooks (confirmed 30 Oct): `PermissionRequest` fires the moment Claude asks for a tool permission (payload `tool_name`, `tool_input`); a `Notification` with `notification_type: permission_prompt` fires only after the prompt has waited about six seconds; `idle_prompt` after about 60 s idle; `elicitation_dialog`, `elicitation_url_dialog`, `agent_needs_input` after about six seconds. A hook that prints nothing changes nothing.
- Reasons an ended row may carry: `process-exited`, `moved-on`, `job-done`, `session-end`. Nothing else validates on the wire.
- Resume commands (own rows only, never on the wire): claude `claude --resume <conversation>`, codex `codex resume <conversation>`, cursor `cursor-agent --resume <conversation>`, opencode `opencode -s <conversation>`, grok `grok -r <conversation>`.
- Copy: `who` status words are exactly `busy`, `waiting for you: <what>`, `idle`, `idle, done`, `offline, last seen <ago>, messages queued`, `parked, wakes when messaged`; the Recent header is `Recent`. The MCP tool description and the docs say the same words.
- Launchd: `setup`, `setup --refresh`, `daemon --install` and `daemon --uninstall` call `launchctl` only when `HOME` is the account's home directory (passwd entry) and `LIGHTBULB_CONNECT_HOME` is unset. Debug builds may force it with `LIGHTBULB_CONNECT_DEBUG_LAUNCHCTL_ANYWHERE=1` (tests).
- Migration number: V16 (`reason`, `waiting_for` on `sessions`). The draft workers branch (`feat/network-workers-rebased`) uses V16/V17 and renumbers when it rebases; note it in that PR.
- Tests: `cargo test` must stay green; new behaviour gets a test in the file named by its task. Release builds never read debug-only env vars.
- Cost: no paid API calls. `claude agents --json --all` takes about 0.25 s; the cache keeps it to at most one run per 10 s plus one per hook event (2 s floor).

## Review Focus

1. **Claude absent or old.** No `claude` on the daemon's PATH (launchd gives `/usr/bin:/bin:/usr/sbin:/sbin`), or Claude older than 2.1.145: the listing is `None` and the pid-file scan must still list every live terminal, as today. Test in Task 5.
2. **A listing that hangs or prints garbage.** `claude agents` must never stall the presence tick: a 3 s timeout, and unparsable output means `None`, not an empty live set that would end every session. Test in Task 5.
3. **A finished background job Claude lists forever.** It must become one ended row, once, and never be re-born as "ended just now" after the seven days. Test in Task 6.
4. **An old peer reading a new presence.** The same frame must parse under the pre-change `Remote`/`Presence` shape (unknown fields ignored) and still pass `valid`. Test in Task 7.
5. **A hook `waiting` versus a stale listing.** A `PermissionRequest` hook sets `waiting` at once; a listing cached up to 10 s earlier still says `busy`. The merged status must be `waiting` until a later hook or a listing `idle` replaces it. Test in Task 5.

---

### Task 1: Launchd is only ever touched in the real home

**Files:**
- Modify: `src/cli.rs:1774-1795` (`use_launchctl`, `launchctl`), `src/cli.rs:1891-1930` (`install_job`), `src/cli.rs:1955-1970` (`daemon_uninstall`), `src/cli.rs:2048-2075` (doctor's "Starts at login")
- Test: `tests/cli.rs` (beside `install_boots_the_job_out_before_it_stops_the_daemon`, line 1085)

**Interfaces:**
- Produces: `pub(crate) fn use_launchctl() -> bool` keeps its name; `pub(crate) fn real_home() -> bool` (HOME equals the passwd home dir and `LIGHTBULB_CONNECT_HOME` is unset); `const LAUNCHCTL_ANYWHERE: &str = "LIGHTBULB_CONNECT_DEBUG_LAUNCHCTL_ANYWHERE"` under `#[cfg(debug_assertions)]`.

- [ ] **Step 1: Write the failing test** in `tests/cli.rs` (macOS only, next to the existing shim test; reuse its shim-writing lines as a small `fn launchctl_shim(sock: &Path) -> (TempDir, PathBuf)` helper):

```rust
#[cfg(target_os = "macos")]
#[test]
fn setup_in_a_temporary_home_never_calls_launchctl() {
    let e = Env::new();
    assert!(e.run(&["who"]).status.success());
    let (shim, log) = launchctl_shim(&e.state.path().join("sock"));
    let path = format!("{}:/usr/bin:/bin", shim.path().display());
    let o = e.cmd(&["daemon", "--install"])
        .env_remove("LIGHTBULB_CONNECT_NO_LAUNCHCTL")
        .env("PATH", path)
        .output().unwrap();
    assert!(o.status.success(), "{}", err(&o));
    assert!(!log.exists(), "launchctl was called from a temp HOME");
    assert!(out(&o).contains("launchctl skipped"), "{}", out(&o));
}
```

- [ ] **Step 2: Run it**: `cargo test --test cli setup_in_a_temporary_home_never_calls_launchctl` → FAIL (the log exists: bootout was called).

- [ ] **Step 3: Implement `real_home()`** in `src/cli.rs` using `libc::getpwuid(libc::getuid())` → `pw_dir`, compared with `std::env::var_os("HOME")` after `std::fs::canonicalize` on both (a missing dir compares as given). `use_launchctl()` = `test_env(NO_LAUNCHCTL).is_none() && (real_home() || anywhere())`, where `anywhere()` is the debug-only env check and `false` in release. The `println!("Background service: launchctl skipped …")` branch already prints the "would run" line; keep it.

- [ ] **Step 4: Update the existing test** `install_boots_the_job_out_before_it_stops_the_daemon` to set `.env("LIGHTBULB_CONNECT_DEBUG_LAUNCHCTL_ANYWHERE", "1")`.

- [ ] **Step 5: Run both**: `cargo test --test cli launchctl` and `cargo test --test cli install_boots` → PASS.

- [ ] **Step 6: Commit**: `git commit -am "setup: launchctl only in the real home (a test run replaced the founder's job)"`.

---

### Task 2: Trust leftovers out of records and receipts

**Files:**
- Modify: `src/presence.rs:425-444` (`record`), `src/mail.rs:14-26` (`HELD_S` doc), `src/mail.rs:188-228` (receipt sentences), `src/mcp/tools.json` (find_sessions description), `src/apps/mod.rs`, `src/apps/ship.rs` (whatever reads `can_send`/`needs_owner_ok`, from `git grep`)
- Test: `tests/directory.rs`, `tests/mcp_tools.rs` (the assertions that name these fields), `tests/mail_ipc.rs` if a receipt test names "allow it first"

**Interfaces:**
- Produces: directory records without `can_send` and `needs_owner_ok`. `State::Held` stays parseable; `HELD_S` stays as the retention window constant with a comment saying no hold exists.

- [ ] **Step 1: Find every reader**: `git grep -n -E "needs_owner_ok|can_send|has to allow it first|waiting for .* to approve" -- src tests docs`. Change the tests first: remove the assertions on those two keys in `tests/directory.rs` and `tests/mcp_tools.rs`, and add to `tests/directory.rs::each_member_sees_the_whole_directory`:

```rust
for r in &on_a { assert!(r.get("can_send").is_none() && r.get("needs_owner_ok").is_none(), "{r}"); }
```

- [ ] **Step 2: Run**: `cargo test --test directory each_member_sees` → FAIL.

- [ ] **Step 3: Remove the two keys** from `record()`, delete the two `"held"` receipt sentences in `mail.rs` (`"held" if remote => …` and `"held" => format!("Sent. {who} has to allow it first …")`), fix the `HELD_S` comment to "A row is kept this long after its final state; nothing is held (owner ruling 2026-10-26)". Fix the apps readers to stop reading the keys.

- [ ] **Step 4: Run**: `cargo test` → PASS.

- [ ] **Step 5: Commit**: `git commit -am "directory: drop needs_owner_ok and can_send; no held receipts (everyone on a network is trusted)"`.

---

### Task 3: Store and session model: waiting, ended with a reason, seven days

**Files:**
- Modify: `src/store.rs:7-9` (MIGRATIONS), `:186-205` (`Session` row struct), `:239-253` (`row_session`), `:315-350` (`put_session`), `:453-470` (`end_session`, `ended_since`), `src/sessions.rs:59-65` (`Status`), `:88-118` (`Session`), `:128-170` (`from_row`), `src/daemon/ipc.rs:1252-1275` (`end`), `:1300-1330` (`register`)
- Test: `src/store.rs` unit tests, `src/sessions.rs` unit tests, `tests/sessions_ipc.rs`

**Interfaces:**
- Produces:
  - `const V16: &str = "ALTER TABLE sessions ADD COLUMN reason TEXT; ALTER TABLE sessions ADD COLUMN waiting_for TEXT;"`, appended to `MIGRATIONS`.
  - `store::Session` gains `pub reason: Option<String>`, `pub waiting_for: Option<String>`; `put_session` writes `waiting_for` and clears `reason` (a live row has none).
  - `pub fn end_session(&self, key: &str, now: i64, reason: &str, title: Option<&str>) -> rusqlite::Result<bool>` (sets `ended_at`, `reason`, and `title` when `Some`).
  - `pub fn ended_since(&self, since: i64, keep: &[&str]) -> rusqlite::Result<Vec<Session>>`: deletes rows with `ended_at < since` whose key is not in `keep`, returns the ended rows (full `Session`), newest `ended_at` first.
  - `sessions::Status` gains `Waiting` (serde `waiting`); `sessions::Session` gains `pub waiting_for: Option<String>`, `pub kind: Option<String>`, `pub entrypoint: Option<String>`, `pub job: Option<Job>` with `pub struct Job { pub id: String, pub state: String }`, all `#[serde(default, skip_serializing_if = "Option::is_none")]`.
  - `pub const WEEK: u64 = 7 * DAY` in `src/presence.rs`.
  - `register` accepts `status: "waiting"` with `waiting_for: Option<String>` (≤ 64 chars, else `bad_request`), stored as `waiting`; `from_row` maps `"live" → Live`, `"waiting" → Waiting`, else `Idle`, and copies `waiting_for`.
  - `end` accepts `reason: Option<String>` (one of the four, default `process-exited`).

- [ ] **Step 1: Write failing store tests** in `src/store.rs` `mod tests` (follow the file's existing test style):

```rust
#[test]
fn ending_keeps_reason_and_title_and_prunes_after_the_window_unless_kept() {
    let s = Store::open(&dir.path().join("state.db")).unwrap();
    s.put_session(&row("k/claude:a")).unwrap();
    s.put_session(&row("k/claude:b")).unwrap();
    assert!(s.end_session("k/claude:a", 1_000, "moved-on", Some("fix the thing")).unwrap());
    assert!(s.end_session("k/claude:b", 1_000, "job-done", None).unwrap());
    let ended = s.ended_since(500, &[]).unwrap();
    assert_eq!(ended.len(), 2);
    assert_eq!((ended[0].reason.as_deref(), ended[0].title.as_deref()), (Some("moved-on"), Some("fix the thing")));
    // Past the window: `a` goes, `b` is kept because the listing still shows it.
    let ended = s.ended_since(2_000, &["k/claude:b"]).unwrap();
    assert_eq!(ended.iter().map(|e| e.key.as_str()).collect::<Vec<_>>(), ["k/claude:b"]);
    assert!(!s.holds("k/claude:a").unwrap());
}

#[test]
fn a_waiting_row_round_trips() { /* put_session with status "waiting", waiting_for "permission prompt"; session() returns both */ }
```

- [ ] **Step 2: Run**: `cargo test store::tests::ending_keeps` → FAIL (signature).

- [ ] **Step 3: Implement** V16, the struct fields, `end_session`, `ended_since` (SQL: `DELETE FROM sessions WHERE ended_at < ?1 AND key NOT IN (…)` built with a `VALUES` list or a per-key check, then `SELECT * … WHERE ended_at IS NOT NULL ORDER BY ended_at DESC`). Update every caller of the two changed functions (`ipc::end`, `presence::build`) to the new signatures; `build()` adapts to `Vec<Session>` by mapping `(key, ended_at, cwd)` for `kept`.

- [ ] **Step 4: Write the failing IPC test** in `tests/sessions_ipc.rs`:

```rust
#[test]
fn register_takes_waiting_with_what_for_and_end_takes_a_reason() {
    let e = Env::new();
    let r = e.ask(json!({"id": 1, "op": "register", "session": {"provider": "claude",
        "conversation": "w1", "pid": pid, "birth_micros": birth, "cwd": "/tmp/p",
        "status": "waiting", "waiting_for": "permission prompt"}}));
    assert_eq!(r["ok"], true, "{r}");
    let s = &e.ask(json!({"id": 1, "op": "sessions"}))["sessions"][0];
    assert_eq!((s["status"].as_str(), s["waiting_for"].as_str()), (Some("waiting"), Some("permission prompt")));
    let r = e.ask(json!({"id": 1, "op": "end", "key": s["key"], "if_gone": false, "reason": "moved-on"}));
    assert_eq!(r["ended"], true, "{r}");
    let r = e.ask(json!({"id": 1, "op": "register", "session": {"provider": "claude", "conversation": "w2",
        "pid": pid, "birth_micros": birth, "cwd": "/tmp/p", "status": "waiting", "waiting_for": "x".repeat(65)}}));
    assert_eq!(r["error"], "bad_request");
}
```

(`end` from a test with no caller and `if_gone: false`: today's code ends the row when neither caller nor `if_gone` gates it; keep that.)

- [ ] **Step 5: Run**: `cargo test --test sessions_ipc register_takes_waiting` → FAIL; implement the `register`/`end` changes; → PASS.

- [ ] **Step 6: Run everything**: `cargo test` → PASS (the `kept` behaviour is unchanged).

- [ ] **Step 7: Commit**: `git commit -am "store: waiting rows, ended rows with a reason and a title, kept seven days (V16)"`.

---

### Task 4: Hooks: the engine is the only status writer, instantly

**Files:**
- Modify: `src/hook.rs:28-63` (`Event`, `EVENTS`), `:171-200` (`run`: status), `:116-120` (`takes_mail`), `src/setup/claude.rs:17-27`, `src/setup/codex.rs:17-35`, `src/setup/SKILL.md` (nothing yet; Task 9)
- Create: `tests/fixtures/hooks/claude-permission-request.json`, `tests/fixtures/hooks/claude-notification-permission.json`, `tests/fixtures/hooks/claude-notification-idle.json`, `tests/fixtures/hooks/codex-permission-request.json`
- Test: `tests/hooks.rs`, `tests/cli.rs` (`setup_installs_the_canonical_binary_and_wires_everything_to_it`, line 629)

**Interfaces:**
- Produces: `Event::Notification` (name `Notification`, arg `notification`), `Event::PermissionRequest` (name `PermissionRequest`, arg `permission-request`); `pub const EVENTS: [Event; 7]` (all), `pub const CODEX_EVENTS: [Event; 6]` (no `Notification`); setup for Claude installs all seven with timeout 2 for `PermissionRequest`, Codex installs `CODEX_EVENTS`. `hook::run` for these two events registers status only: no mail, no capture, prints nothing.

- [ ] **Step 1: Fixtures.** Claude `PermissionRequest`: `{"session_id":"00000000-0000-4000-8000-0000000000c1","transcript_path":"…","cwd":"/tmp/proj","permission_mode":"default","hook_event_name":"PermissionRequest","tool_name":"Bash","tool_input":{"command":"rm -rf node_modules"}}`. Claude `Notification` permission: same common fields plus `"hook_event_name":"Notification","message":"Claude needs your permission","title":"Permission needed","notification_type":"permission_prompt"`; idle: `"notification_type":"idle_prompt"`. Codex `PermissionRequest`: `{"session_id":"019a…","transcript_path":"…","cwd":"/tmp/proj","hook_event_name":"PermissionRequest","model":"gpt-5"}`.

- [ ] **Step 2: Write failing tests** in `tests/hooks.rs` (use its `hook(&self, agent, event, fixture)` helper):

```rust
#[test]
fn a_permission_request_marks_the_session_waiting_and_prints_nothing() {
    let e = HookEnv::new();   // whatever the file's env type is called
    e.hook("claude", "session-start", "claude-session-start.json");
    let out = e.hook("claude", "permission-request", "claude-permission-request.json");
    assert_eq!(out.trim(), "");
    let s = e.session_row();  // the file's way of reading the registered row
    assert_eq!((s["status"].as_str(), s["waiting_for"].as_str()), (Some("waiting"), Some("permission prompt")));
    e.hook("claude", "post-tool-use", "claude-post-tool-use.json");
    assert_eq!(e.session_row()["status"], "live");
}

#[test]
fn notifications_set_waiting_or_idle_by_type() {
    // permission_prompt -> waiting "permission prompt"; idle_prompt -> idle; auth_success -> row unchanged
}

#[test]
fn a_codex_permission_request_marks_the_thread_waiting() { /* same with the codex fixture */ }
```

- [ ] **Step 3: Run**: `cargo test --test hooks permission` → FAIL (unknown event).

- [ ] **Step 4: Implement.** In `normalize`, the two names resolve like the others. In `run`, before the mail path: `match h.event { PermissionRequest => ("waiting", Some("permission prompt")), Notification => match payload["notification_type"] { "permission_prompt" => ("waiting", Some("permission prompt")), "elicitation_dialog" | "elicitation_url_dialog" | "agent_needs_input" => ("waiting", Some("input needed")), "idle_prompt" => ("idle", None), _ => return Ok(None) }, UserPromptSubmit | PostToolUse => ("busy", None), _ => ("idle", None) }`; the `register` body carries `waiting_for`; after `register`, these two events `return Ok(None)` (no capture, no inbox). `takes_mail` returns `false` for them.

- [ ] **Step 5: Setup.** `setup/claude.rs` loops `EVENTS` with `timeout: 2` for `PermissionRequest`, 1 for `SessionEnd`, 5 otherwise; `setup/codex.rs` loops `CODEX_EVENTS`. Extend `tests/cli.rs::setup_installs_the_canonical_binary_and_wires_everything_to_it` to assert `~/.claude/settings.json` has `hooks.PermissionRequest` and `hooks.Notification` groups with our command, and `~/.codex/hooks.json` has `PermissionRequest` and no `Notification`.

- [ ] **Step 6: Run**: `cargo test --test hooks` and `cargo test --test cli setup_installs` → PASS.

- [ ] **Step 7: Commit**: `git commit -am "hooks: PermissionRequest and Notification write waiting at once (one status writer)"`.

---

### Task 5: Claude discovery reads `claude agents --json --all`

**Files:**
- Create: `src/agents/listing.rs`
- Modify: `src/agents/mod.rs` (add `pub mod listing;`), `src/sessions.rs:175-236` (`scan_claude` stays, used for the push socket and as fallback), `:243-267` (`all`), `src/daemon/mod.rs:55-110` (a `listing` cache field), `src/daemon/ipc.rs:1408-1424` (`live_sessions`), `:1300-1330`, `:1252-1275` (`register`/`end` mark the cache dirty)
- Create: `tests/fixtures/claude-agents/five.json` (the five-row shape observed on 30 Oct: four interactive, one desktop-app; plus one `background` `working` with a pid, one `background` `blocked` with `waitingFor: "permission prompt"`, one `background` `done` with a pid, one `background` `stopped` without a pid)
- Test: `src/agents/listing.rs` unit tests, `tests/sessions_ipc.rs`

**Interfaces:**
- Produces:
  - `pub struct Listed { pub conversation: String, pub pid: Option<u32>, pub kind: String, pub status: Option<String>, pub waiting_for: Option<String>, pub state: Option<String>, pub job_id: Option<String>, pub name: Option<String>, pub cwd: String, pub started_ms: u64 }`
  - `pub fn parse(text: &str) -> Option<Vec<Listed>>` (`None` when not a JSON array; rows without a UUID `sessionId` skipped).
  - `pub fn claude_exe(home: &Path) -> Option<PathBuf>`: in order, the executable of a live process named in `<home>/.claude/sessions/*.json` (`os::process(pid).executable`), then `<home>/.local/bin/claude` if it exists, then the first `claude` on `PATH`.
  - `pub fn read(exe: &Path) -> Option<Vec<Listed>>`: runs `exe agents --json --all` with `HOME` unchanged, kills it after 3 s, `None` on non-zero exit, timeout or unparsable output.
  - `pub fn to_sessions(listed: &[Listed], member_short: &str, sockets: &HashMap<String, PathBuf>) -> Vec<Session>`: a live `Session` per row with a `pid` (status `busy → Live`, `waiting → Waiting` with `waiting_for`, `idle → Idle`; background `working → Live`, `blocked → Waiting` with `waiting_for` or `"blocked"`, `done | failed | stopped → Idle` with `job: Some(Job { id, state })`); `kind` and `entrypoint` filled (`entrypoint` from the pid file when present); `tier: Push` when `sockets` has the conversation's `messagingSocketPath`; rows without a pid are not returned here (Task 6 ends them).
  - `pub fn finished(listed: &[Listed]) -> Vec<String>`: conversations listed with `state` in `done | failed | stopped` and no pid (for Task 6's `job-done` and `keep`).
  - Daemon: `pub listing: Mutex<ListingCache>` with `pub struct ListingCache { pub at: Option<Instant>, pub rows: Option<Vec<Listed>>, pub dirty: bool }`; `pub(crate) fn current(daemon: &Daemon) -> Option<Vec<Listed>>` refreshes when `rows` is `None`, or `at` is older than 10 s, or `dirty && at` older than 2 s; `register` and `end` set `dirty = true`.
  - `sessions::all(rows, listed: Option<&[Listed]>, home, member_short) -> Vec<Session>`: with `Some`, Claude sessions come from `to_sessions` (the pid-file scan only supplies sockets); with `None`, from `scan_claude` as today. Merge with hook rows as today, except: a hook row whose status is `waiting` keeps `Waiting` unless the listing says `idle`.

- [ ] **Step 1: Write failing unit tests** in `src/agents/listing.rs`:

```rust
#[test]
fn parse_maps_claudes_words_and_skips_what_has_no_pid() {
    let rows = parse(include_str!("../../tests/fixtures/claude-agents/five.json")).unwrap();
    let live = to_sessions(&rows, "k3f9ab7cd2ef", &HashMap::new());
    let by = |c: &str| live.iter().find(|s| s.conversation == c).unwrap();
    assert_eq!(by("…interactive busy…").status, Status::Live);
    assert_eq!((by("…blocked…").status, by("…blocked…").waiting_for.as_deref()), (Status::Waiting, Some("permission prompt")));
    assert_eq!(by("…done…").job.as_ref().map(|j| j.state.as_str()), Some("done"));
    assert!(live.iter().all(|s| s.conversation != "…stopped no pid…"));
    assert_eq!(finished(&rows), vec!["…done…".to_string(), "…stopped no pid…".to_string()]);
}

#[test]
fn garbage_and_non_arrays_are_none() { assert!(parse("not json").is_none()); assert!(parse("{}").is_none()); }

#[test]
fn a_hanging_claude_is_none_within_three_seconds() { /* `read` on a shell script that sleeps 10 */ }
```

- [ ] **Step 2: Run**: `cargo test listing::` → FAIL.

- [ ] **Step 3: Implement** `listing.rs`, the daemon cache, and the `all()` signature change. In `live_sessions`, call `listing::current(daemon)` (it needs `claude_exe(&daemon.claude_home)`; a `None` exe means `None` rows) and pass it to `all`.

- [ ] **Step 4: Write the failing integration test** in `tests/sessions_ipc.rs`: a fake `claude` script on `PATH` (shim pattern from `tests/cli.rs:1085`) that prints `five.json`; start the daemon with that `PATH`; `sessions` lists the live rows with `status: "waiting"` and `job.state: "done"`; then replace the script with one that exits 1 and assert the pid-file-scan fallback still lists the test's own registered Claude session (register one first, as `tests/directory.rs::register` does). Name: `the_listing_feeds_sessions_and_a_failing_claude_falls_back_to_pid_files`.

- [ ] **Step 5: Write the stale-listing test** (Review Focus 5) in the same file: register a claude session `waiting` through `register`, with the shim listing it as `busy`; `sessions` says `waiting`; register it `busy` (a PostToolUse); `sessions` says `live`. Name: `a_hook_waiting_stands_over_a_stale_listing`.

- [ ] **Step 6: Run**: `cargo test --test sessions_ipc` → PASS. `cargo test` → PASS (`scan_claude` tests unchanged).

- [ ] **Step 7: Commit**: `git commit -am "sessions: Claude discovery from claude agents --json --all, pid files as the push socket and fallback"`.

---

### Task 6: Ending: every session seen gets a row, and a gone one ends with a reason

**Files:**
- Modify: `src/daemon/ipc.rs:1408-1424` (`live_sessions`), `src/doing.rs` (add `continued_in`), `src/hook.rs:140-158` (SessionEnd passes a reason), `src/presence.rs:147-211` (`build`: ended rows and `WEEK`), `:395-420` (`stored`: `WEEK`)
- Test: `src/doing.rs` unit tests, `tests/sessions_ipc.rs`, `tests/hooks.rs`

**Interfaces:**
- Consumes: Task 3's `end_session`/`ended_since`, Task 5's `current`/`finished`.
- Produces:
  - `pub fn continued_in(home: &Path, s: &Session) -> bool` in `doing.rs`: the transcript tail's last `continued-in` row has no `user` or `assistant` row after it.
  - `live_sessions` becomes the one place that writes rows: after `all()`, for each live Claude session with no store row and a publishable cwd (`!presence::hidden`), `put_session`; for each live store row (not ended) whose key is not in the live set, `end_session(key, now, reason, title)` where `reason` is `job-done` if `finished(listing)` names its conversation, else `process-exited` if the row's pid is no longer alive (`sessions::alive`), else `moved-on` (the process lives but the harness left the conversation: `/clear`, `/resume`, the agent view; `continued_in` is corroboration, not a condition), and `title` is `doing::title_and_todo` on a `Session` built from the row. Then `daemon.presence.notify_one()` when anything changed.
  - `pub(crate) fn ended_sessions(daemon: &Daemon) -> Vec<store::Session>`: `ended_since(now - WEEK, &finished conversations' keys)`, hidden folders filtered out, newest first.
  - The SessionEnd hook calls `end` with `reason`: Claude payload `reason` `clear | resume → moved-on`, anything else → `session-end`; Codex, Cursor, Grok, OpenCode → `session-end`.
  - `sessions` op answers `{ sessions, ended }` where `ended` is `ended_sessions` as JSON rows plus `resume` (the provider's command with the conversation).

- [ ] **Step 1: Write failing tests.** `src/doing.rs`:

```rust
#[test]
fn continued_in_is_true_only_when_nothing_followed_it() {
    // tail "…\n{\"type\":\"continued-in\",\"continuedInSessionId\":\"x\"}\n" -> true
    // the same followed by a {"type":"user"} row -> false
}
```

`tests/sessions_ipc.rs`:

```rust
#[test]
fn a_listed_session_that_disappears_becomes_an_ended_row_with_its_title() {
    // shim lists conversation A (interactive, pid = this test's pid) with a transcript fixture
    // holding an ai-title; `sessions` lists it live; the shim stops listing it and the daemon is
    // told to refresh (register anything, which marks the cache dirty; wait > 2 s).
    // The pid (this test) is alive, so the listing dropping A means Claude moved on:
    // `sessions`.ended has A with reason "moved-on", title == the fixture's aiTitle and
    // resume == "claude --resume <A>". A second case: the listing drops a row whose pid is a
    // dead one (register with a pid that exited) -> reason "process-exited".
}

#[test]
fn a_finished_job_ends_once_and_is_not_reborn() {
    // shim lists job J as state "done" without a pid; `sessions`.ended has J with "job-done";
    // call `sessions` again 3 s later: still exactly one J, same ended_at.
}
```

`tests/hooks.rs`: `fn a_session_end_after_clear_ends_the_row_as_moved_on()` using a copy of `claude-session-end.json` with `"reason":"clear"`.

- [ ] **Step 2: Run**: the three → FAIL.

- [ ] **Step 3: Implement** as in Interfaces. `build()` reads `ended_sessions(d)` for the `ended` frame list (Task 7 adds the frame type; until then keep the result in a local and leave `kept` as it is) and uses `WEEK` in `stored()`.

- [ ] **Step 4: Run**: `cargo test` → PASS.

- [ ] **Step 5: Commit**: `git commit -am "sessions: every session seen has a row; a gone one ends with a reason and its title"`.

---

### Task 7: Wire: `waiting_for` on `Remote`, `ended` on `Presence`

**Files:**
- Modify: `src/net/frames.rs:27-95` (`Remote`, new `Ended`), `:123-131` (`Presence`), `:302-317` (`from_peer_ok`), `src/presence.rs:31-39` (`Latest`), `:55-78` (`load`), `:80-104` (`remote`), `:147-211` (`build`), `:213-230` (`same`), `:340-380` (`put`), `tests/common/mod.rs:594-620` (`remote`, `presence_with`)
- Test: `src/net/frames.rs` unit tests, `tests/directory.rs`

**Interfaces:**
- Produces:
  - `Remote` gains `#[serde(default, skip_serializing_if = "Option::is_none")] pub waiting_for: Option<String>`; `valid` also requires it `short`.
  - `pub struct Ended { pub session_id: String, pub agent: String, pub folder: String, pub title: Option<String>, pub repo: Option<String>, pub branch: Option<String>, pub ended_at: u64, pub reason: String }` with `pub fn valid(&self, peer_short: &str) -> bool` (own address, `agent` is the id's provider, `reason` in `pub const ENDED_REASONS: [&str; 4]`, texts short, `ended_at <= now + 86_400`) and `clamp()`.
  - `Frame::Presence` gains `#[serde(default, skip_serializing_if = "Vec::is_empty")] ended: Vec<Ended>`; `from_peer_ok` checks `ended.len() <= 50` and each `valid`.
  - `presence::remote` maps `Status::Waiting` to `status: "busy"` with `waiting_for` set; `Latest` gains `pub ended: Vec<Ended>`; `build` fills `ended` from `ended_sessions(d)` (newest first, at most 50, `folder` as `remote` computes it, `kept` unchanged); `put`/`load`/`same` carry `ended`.

- [ ] **Step 1: Write failing unit tests** in `src/net/frames.rs`:

```rust
#[test]
fn an_old_build_reads_a_new_presence_and_a_new_build_reads_an_old_one() {
    // Old shape: a serde struct declared in the test with the pre-change fields only, no
    // deny_unknown_fields. Serialize a new Presence with waiting_for and ended; deserialize
    // into the old shape: Ok, and its sessions still `valid`. Then an old JSON text (no
    // waiting_for, no ended) deserializes into Frame::Presence with ended == [] and
    // waiting_for == None.
}

#[test]
fn a_waiting_session_travels_as_busy_with_what_for() { /* presence::remote on Status::Waiting */ }

#[test]
fn ended_validates_reason_owner_and_text() { /* bad reason, foreign prefix, long title -> false */ }
```

- [ ] **Step 2: Run**: `cargo test frames::tests` → FAIL.

- [ ] **Step 3: Implement** the structs and the plumbing; update `tests/common/mod.rs::remote` to set `waiting_for: None` and `presence_with` to pass `ended: vec![]`.

- [ ] **Step 4: Integration test** in `tests/directory.rs`: `fn a_teammates_ended_session_is_seen_for_a_week_and_never_its_resume()` (two daemons; A registers claude `a1` and ends it through `end` with `reason: "session-end"`; `until` B's `directory` lists `a1` with `status: "ended"`, `reason`, `ended_at`, no `resume`; A's own `directory` row has `resume == "claude --resume a1"`). Also `fn a_presence_with_fifty_one_ended_rows_is_dropped()` using `presence_with`-style raw frames.

- [ ] **Step 5: Run**: `cargo test --test directory` → PASS; `cargo test` → PASS.

- [ ] **Step 6: Commit**: `git commit -am "wire: waiting_for on sessions, ended sessions on presence (optional fields, old peers unaffected)"`.

---

### Task 8: The directory record and `find_sessions`

**Files:**
- Modify: `src/presence.rs:425-525` (`record`, `directory`), `src/daemon/ipc.rs:289-300` (`sessions` op answers `ended`), `src/mcp.rs:709-735` (`find_sessions`), `src/mcp/tools.json` (find_sessions description)
- Test: `tests/directory.rs`, `tests/mcp_tools.rs`

**Interfaces:**
- Consumes: Task 6's `ended_sessions`, Task 7's `Latest.ended`.
- Produces: every `directory` record has `session_id, person, person_id, person_verified, computer, agent, folder, cwd (own only, else ""), repo, branch, status (busy | waiting | idle | ended), waiting_for?, doing, title, tier, online, seen_at? (unix s, only when online is false), wakes (bool), user_id? (a claim: the `sub` of this computer's `login.json` proof via `proof::peek`, or of the peer's `hello` proof in `d.proofs`), ended_at?, reason?, resume? (own ended rows only)`; own live rows also `handle, hidden, pid, birth_micros, kind, entrypoint, job`. Order: own live, own ended, then per member live then ended. `wakes` is `d.team.lock().roster.wakes.contains_key(&m.key)`. `find_sessions` passes the records through (renaming `handle` → `name` as today) and its description in `tools.json` becomes:

  > The coding sessions you can message on your team's lightbulb network (Claude Code, Codex, Cursor, Grok CLI, OpenCode): this computer's, then your teammates'. status is busy, waiting (waiting_for says for what; its person must answer), idle, or ended. Ended sessions stay seven days with ended_at and reason; this computer's carry resume, the command that resumes them. online false with seen_at is a computer that is offline (messages wait); wakes true is a parked workspace that a message wakes. Pass one's session_id as send_message session_id; is_this_session marks your own: never message it.

- [ ] **Step 1: Failing test** in `tests/directory.rs::each_member_sees_the_whole_directory`: extend the existing assertions: B's row on A has `folder == "web"`, `wakes == false`, no `seen_at` while online; after B's daemon stops (`e.ask shutdown`), `until` A shows it `online == false` with a `seen_at` within 60 s of now. And in `tests/login.rs` or `tests/directory.rs`: a signed-in A (`Env::sign_in`) shows `user_id == "user_…"` on its own row and B sees the same `user_id` on A's rows.

- [ ] **Step 2: Run** → FAIL; implement; → PASS.

- [ ] **Step 3: MCP test** in `tests/mcp_tools.rs`: `find_sessions` on a daemon with one ended own row returns it with `status: "ended"` and `resume`, after the live ones, and `total` counts both.

- [ ] **Step 4: Run**: `cargo test --test mcp_tools` → PASS; `lightbulb mcp --tools-json` prints the new description (checked by the existing tools.json test if any).

- [ ] **Step 5: Commit**: `git commit -am "directory: waiting, ended, offline since, parked, folder and user_id on every record"`.

---

### Task 9: `who` says what is true

**Files:**
- Modify: `src/cli.rs:196-300` (`who`)
- Test: `tests/cli.rs`

**Interfaces:**
- Consumes: Task 8's records; the `sessions` op's `ended`.
- Produces: Status cell: `busy`; `waiting for you: <waiting_for>` (own) or `waiting: <waiting_for>` (a teammate's); `idle`; `idle, done` when `job.state` is `done | failed | stopped`; then `, gets messages instantly | at its next step | when it checks` as today and `, hidden from teammates` as today. A teammate's row with `online == false`: `offline, last seen <ago>, messages queued` and nothing else; with `wakes == true` and `online == false`: `parked, wakes when messaged`. After the table, when there are ended rows: a blank line, `Recent`, and a table with `Name` (handle, else session id), `Person` and `Computer` (with `--all`), `Agent`, `Folder`, `Ended` (`<ago>`), `Resume` (own rows: the command; a teammate's: empty). `fn ago(secs_ago: u64) -> String`: `just now` under 60 s, `N min ago`, `N h ago`, `N d ago`. `--json` prints `{sessions, ended}` for `who` and the directory's array for `who --all` as today.

- [ ] **Step 1: Failing tests** in `tests/cli.rs`:

```rust
#[test]
fn who_prints_waiting_done_and_a_recent_section_with_resume() {
    let e = Env::new();
    // register claude c1 waiting "permission prompt"; register codex t2 then end it "session-end"
    let table = out(&e.run(&["who"]));
    assert!(table.lines().any(|l| l.contains("waiting for you: permission prompt")), "{table}");
    let recent = table.split("\nRecent\n").nth(1).expect("a Recent section");
    assert!(recent.contains("codex-1") && recent.contains("just now") && recent.contains("codex resume t2"), "{recent}");
}

#[test]
fn who_all_prints_busy_for_a_busy_teammate_and_offline_with_last_seen() { /* two daemons; was always "idle" (cli.rs:271) */ }
```

- [ ] **Step 2: Run** → FAIL; implement; → PASS. `cargo test --test cli` → PASS.

- [ ] **Step 3: Commit**: `git commit -am "who: busy, waiting for you, idle done, offline since, parked, and a Recent section with resume"`.

---

### Task 10: Docs, skill, version

**Files:**
- Modify: `docs/usage.md` (the `who` row and the "teammates see" paragraph near line 149), `docs/security.md` (what teammates see: add ended sessions' names and folders for seven days; nothing else widens), `docs/agents.md` (find_sessions fields), `src/setup/SKILL.md:11` (find_sessions: ended rows, waiting_for, offline), `docs/verification.md` (one row per task's test: launchctl guard, waiting hooks, listing, ended rows, wire compat, directory fields, who), `Cargo.toml` + `Cargo.lock` (`0.8.0`)
- Test: `cargo test` (docs content tests if any), `cargo build --release` once.

- [ ] **Step 1: Write the doc changes** in the same plain voice as the files (no competitor named, no "honest" marketing; say what is shown and for how long).
- [ ] **Step 2: Verify `lightbulb update` re-runs setup** so existing installs get the two new hooks: read `src/cli.rs` `update`; if it only swaps the binary and restarts the job, add a `setup --refresh` call that rewrites hooks (not launchd) and note it in `docs/usage.md`. Record the finding in the PR body.
- [ ] **Step 3: Bump** `version = "0.8.0"` (`cargo update -p lightbulb` for the lock).
- [ ] **Step 4: Run**: `cargo fmt --check`, `cargo clippy --all-targets`, `cargo test` → clean.
- [ ] **Step 5: Commit**: `git commit -am "docs: ended sessions, waiting, offline; 0.8.0"`.

---

## After this plan (not tasks here)

- Open the PR on the CLI repo with the `pr` skill's body; CI green on all four runners; the founder merges and tags `v0.8.0` (signed release via the monorepo `sign-cli-release.yml`).
- Monorepo follow-ups, in the Step 3 plan: Hermes Dockerfile pin → v0.8.0 with its checksum, `hermes-version.json` flip, deploy, `roll_hermes_runtimes`; re-vendor `tools.json` into the hosted MCP; amend `docs/superpowers/specs/2026-10-22-lightbulb-network-architecture.md` §6, §9, §11 with the 26 Oct ruling.
- Acceptance A1-A4 from the spec, run on the founder's and Aden's Macs after the release: record the outputs in `docs/verification.md`'s "Real computers".
