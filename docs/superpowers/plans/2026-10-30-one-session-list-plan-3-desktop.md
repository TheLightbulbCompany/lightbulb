# One session list, Step 3: the desktop app and backend read the engine

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** On a desktop with engine ≥ 0.8.0, Claude mirrors exist, change state and end only because the engine says so; every mirror carries `networkSessionId`; the sidebar joins backend and engine rows and shows network-only rows.

**Architecture:** A poller asks the engine's `sessions` op every 2 s (native, unfiltered) and hands the answer to `Host`, whose `apply_engine` replaces the Claude half of `liveness_tick` and writes status. Full capture keeps running per live row on the 60 s backstop or a wake, fed by engine rows shaped like listing rows. The page polls `engine_ask("directory")` every 5 s and `mergeSessionRows` does the join. Everything else on the relay is untouched.

**Tech Stack:** Rust (Tauri, tokio), Python (FastAPI, pydantic), TypeScript (React, vitest).

**Spec:** `docs/superpowers/specs/2026-10-30-one-session-list-design.md` §4.3, §4.4, §6 A5-A7 A10, §8, §10. Engine shapes: `origin/feat/one-session-list` in `~/Desktop/lightbulb-network.nosync`, `src/daemon/ipc.rs:291-306,1711-1760`, `src/sessions.rs:92-136`.

## Global Constraints

- Worktree `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.claude/worktrees/step3-sidebar-data`. Never `git stash`. Never touch another worktree. Never replace or restart the person's real `~/.lightbulb/connect` service.
- No full suites. Focused tests only (`cargo test --lib <filter>`, `vitest run <file>`, `pytest <file> -k <expr>`). Delete `apps/desktop/src-tauri/target` if it exceeds 6 GB.
- Page script never joins or leaves a network and never sees `cwd` or `resume`. `engine_ask(op)` stays read-only with no body.
- Without `engine_ask`, or with an engine older than 0.8.0, the page behaves exactly as today. Web and phone unchanged in shape. `networkSessionId` optional everywhere.
- `SessionRow`'s shape does not change. Step 4 owns the layout of `RoomDirectory`'s Sessions section, `SessionListRow`, `AllSessionsView`: only the smallest data-source edits there.
- Codex, Cursor, Grok discovery, status and end are unchanged. Only `provider == "claude"` loses its liveness and hook status.
- Shared-session lifecycle reads the backend row: done/background, waiting-approval with `pendingApproval`, join, pick up, resume, PR, agents, unread, archive.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Engine unreachable or restarting: no mirror is ended by silence; after 3 failed polls Claude mirrors read `unknown`, not `idle`; a dead pid ends. Tested in Task 3.
2. A live engine row whose capture always fails (a spare, a non-UUID id): must not spin discovery. Tested in Task 3.
3. A held permission prompt while the engine says `busy`: status stays `waiting-approval` until released. Tested in Task 3.
4. New web on an old app, or new app on a 0.7.x engine: no fault shown, no network rows, today's list. Tested in Task 5.
5. An older relay that does not list `networkSessionId`: the frame must not carry it, since `extra="forbid"` rejects the whole frame. Tested in Task 3.

---

### Task 1: Backend `networkSessionId`

**Files:** Modify `apps/backend/core/services/local_sessions.py`, `apps/backend/core/services/local_session_relay.py`. Test: the existing relay and session test files beside them (find with `git grep -l "sessionSignals" apps/backend/tests`).

**Interfaces:** Produces the wire field `networkSessionId` on `local.session.register`'s `session` and on each heartbeat `sessions[]` entry, listed in `FEATURES` as `"networkSessionId"`, public on `LocalSession`.

- [ ] Write failing tests: a register with `networkSessionId: "abc123def456/claude:11111111-1111-4111-8111-111111111111"` returns it on the public row; a heartbeat carrying it adds it to a row registered without it; a heartbeat without it leaves it in place; a value failing the pattern is rejected; a row without it serialises with no such field (or null per the model's existing optional convention); `"networkSessionId" in FEATURES`; `nativeConversationId` and `profileFingerprint` still absent from the public row.
- [ ] Run them, expect FAIL.
- [ ] Add `network_session_id: Annotated[str, Field(max_length=256, pattern=r"^[A-Za-z0-9._-]{1,64}/[a-z]+:[A-Za-z0-9._-]{1,128}$")] | None = None` to `Descriptor` and `HeartbeatSession` (relay) and `SessionRegistration`, `LocalSession` (service). Write it in the heartbeat via the existing `labels` mapping (non-None only). Make sure the register path carries it to the stored row, including a re-register that does not name it (keep the previous value).
- [ ] Run them, expect PASS. Commit `backend: networkSessionId on local sessions, stored and public`.

### Task 2: Reading the engine (pure) and capture from engine rows

**Files:** Create `apps/desktop/src-tauri/src/local_sessions/engine_view.rs`. Modify `local_sessions.rs` (mod line), `local_sessions/claude.rs`, `engine_client.rs` (drop `allow(dead_code)`; add `own`).

**Interfaces, produced:**
```rust
pub(super) struct EngineRow { key: String, provider: String, conversation: String, pid: u32, birth_micros: u64, cwd: String, status: String /* desktop word */, waiting_for: Option<String>, kind: Option<String>, job: Option<(String, String)> /* id, state */ }
pub(super) struct EngineEnded { key: String, provider: String, conversation: String, reason: Option<String> }
pub(super) struct EngineView { live: Vec<EngineRow>, ended: Vec<EngineEnded> }
pub(super) fn parse_sessions(answer: &Value) -> Result<EngineView, String>;
pub(super) fn desktop_status(engine: &str) -> &'static str;
pub(super) fn ended_reason(engine: Option<&str>) -> &'static str;
pub(super) fn listing_row(row: &EngineRow) -> Value;
pub(super) async fn read() -> Result<EngineView, String>; // engine_client::ask("sessions", None) then parse_sessions
```
`claude::discover(home)` keeps its signature and `Discovery`, minus nothing; its rows now come from `engine_view::read()`.

- [ ] Failing tests in `engine_view.rs`:
  - `desktop_status`: `live`→`busy`, `waiting`→`waiting-approval`, `idle`→`idle`, anything else→`unknown`.
  - `ended_reason`: `process-exited` and `None`→`process-exited`; `moved-on`, `session-end`, `job-done`→`exited`. Every output is in the backend's `EndedReason`.
  - `parse_sessions`: `{ok:false,error}`→`Err`; an object with `sessions` and no `ended` array→`Err` containing `older than 0.8.0`; rows with no `pid`, or a null `conversation` on an ended row, are skipped; a real-shaped row parses (fixture: `{"key":"abc/claude:U","provider":"claude","conversation":"U","pid":7,"birth_micros":9,"cwd":"/x","status":"live","tier":"push","kind":"background","entrypoint":"cli","job":{"id":"deadbeef","state":"working"}}`).
  - `listing_row`: gives `kind` (default `interactive`), `id` (job id), `pid`, `sessionId`, `cwd`, `state` (job state), `waitingFor`, `status` such that `claude::listing_status` returns the row's desktop word. Finished states `done|failed|stopped` return `idle`; `blocked` engine rows return `waiting-approval` (engine status `waiting` → listing `waiting`).
- [ ] Failing test in `claude.rs` tests: `attention` with `state` `failed` or `stopped` sets `done: true`; `blocked` with a `needs` note keeps `note` even when status is `waiting`.
- [ ] Failing test in `engine_client.rs`: `page_record` adds `own: true` when the record has a `pid` or a `resume` key (even null) and nothing for a teammate's record; extend the existing exact-JSON test.
- [ ] Run, expect FAIL.
- [ ] Implement. `claude::discover`: `let view = engine_view::read().await?;` keep only `provider == "claude"` rows, `capture` each `listing_row`, `conversations` and `jobs` from them via the existing `listed`. Keep `provider_program` for the version check. `attention`: `done` is `matches!(state, done|failed|stopped)`; `note` when `state == blocked`. `listing()`, `running_conversations`, `background_job`, `Holders`, `still_registered` stay: Resume and End read them before acting.
- [ ] Run `cargo test --lib engine_view` and `cargo test --lib engine_client`, plus `claude::` tests. PASS. Commit `desktop: read Claude sessions from the engine`.

### Task 3: `Host` takes state and end from the engine

**Files:** Modify `apps/desktop/src-tauri/src/local_sessions/transport.rs`, `lifecycle.rs` if needed, `local_sessions.rs` (`Mirror.network_id`).

**Interfaces, consumed:** Task 2's `EngineView`, `ended_reason`, `read`. **Produced:** `Host::apply_engine(&mut self, answer: &Result<EngineView, String>)`; `Mirror.network_id: Option<String>` (`#[serde(default)]`); frame field `networkSessionId`.

Behaviour of `apply_engine`, `Ok(view)`:
- every mirror, any provider, not ended or ended: set `network_id` to the `key` of the live or ended row with the same `(provider, conversation)`; a change marks it heartbeat-dirty.
- Claude mirror not ended, row live: `native.status = row.status` unless `mirror.waiting` has an open non-daemon `held` (then `waiting-approval`); `attention.done` from the job state; `attention.waiting_for` from `row.waiting_for` when waiting; `note` left alone. A change persists and goes in `heartbeat_dirty`.
- Claude mirror not ended, no live row: `end_session(key, reason)`, reason `on-purpose` when `left_on_purpose` holds the key, else `ended_reason(ended row's reason)`, `process-exited` when there is no ended row.
- a live Claude row with no not-ended mirror at the same pid and birth: `rediscover.notify_one()` once per `(conversation, pid, birth)`, remembered in a `BTreeSet` pruned to current live rows.

`Err(_)`: every Claude mirror not ended: `status = "unknown"` (held rule still applies); `still_running` false ends it `process-exited`. Non-Claude untouched.

- [ ] Failing tests, one each, with `test_mirror`:
  - idle row → `idle`; `waiting` → `waiting-approval` and `waitingFor`; busy → `busy`; a finished job → `idle` with `attention.done`.
  - ended row `moved-on` → mirror ended `exited`; `session-end` → `exited`; `process-exited` → `process-exited`; absent → `process-exited`.
  - hook `SessionEnd` `prompt_input_exit` then the row ends → `on-purpose`; the hook alone ends nothing for Claude.
  - hook `UserPromptSubmit`/`Stop`/`Notification` on Claude: `native.status` unchanged, an open un-held `waiting` is still cleared, `last_assistant_message` still stored. On Codex: unchanged behaviour.
  - held prompt + engine `busy` → `waiting-approval`; released → `busy` next apply.
  - `Err`: nothing ended while the pid lives; status `unknown`; a dead pid ended `process-exited`.
  - a row with no mirror notifies `rediscover` exactly once across repeated applies.
  - a Codex mirror with a matching row gets `network_id` and keeps its status.
  - `registration` and the heartbeat entry carry `networkSessionId` only when the relay's features list it and `network_id` is set.
- [ ] Run, expect FAIL.
- [ ] Implement:
  - Poller, a third future beside `discovery` and `network` in `run` (`transport.rs:1559` area): every 2 s `engine_view::read()`; send through a `tokio::sync::watch` to `Host`. `Err` is sent only after 3 consecutive failures; a single failure sends nothing. The connection tick calls `apply_engine` when the watch changed, beside `liveness_tick` (`transport.rs:1794`, `1990`).
  - `liveness_tick`: Claude mirrors are skipped. Delete `held`, `unheld`, `JOB_HANDOVER` and the Claude branch of `runs`.
  - `observe`: delete `moved` (fork ends origin) and the `claude_listed` parameter; drop it from `Discovered`. Limit the `unknown → idle` rule to non-Claude (`transport.rs:511-527`). `returned_claude` keeps `still_registered`: it guards a revive on the same pid while an End is in flight.
  - `apply_hook`, `provider == "claude"`: `Transition::Status` writes no `native.status` and otherwise runs as today. `Transition::Waiting` writes `waiting-approval` only when the prompt is held. `Transition::Ended`: `ON_PURPOSE` adds to `left_on_purpose`, then nothing else.
  - Feature `networkSessionId` read beside the others (`session_states::read_features` pattern).
  - Migrate or delete the existing tests of removed rules. A test of a removed rule goes; a test of a kept behaviour moves to `apply_engine`.
- [ ] Run `cargo test --lib local_sessions::transport` and `cargo test --lib lifecycle`. PASS. Commit `desktop: a Claude mirror's state and end come from the engine`.

### Task 4: Native commands: Share and Fix

**Files:** Modify `apps/desktop/src-tauri/src/engine.rs`, `local_sessions/enrollment.rs` or `transport.rs`, `lib.rs`, `tests/invoke_surface.rs`.

**Interfaces, produced:** Tauri `share_engine_session(app, session_id: String) -> Result<(), String>`; Tauri `engine_fix() -> Result<(), String>`.

- [ ] Failing tests:
  - a pure `own_claude_conversation(view: &EngineView, session_id: &str) -> Option<String>`: returns the conversation only for a live `claude` row whose `key` equals `session_id`. A teammate's id, an ended row, a Codex row, garbage → `None`.
  - `engine::fix_at(home, bundled, limit)` runs `setup` even when `setup-version` is current (fake engine helper in `engine.rs` tests).
  - `invoke_surface`: `share_engine_session` listed with `Some("confirm_native(")`; `engine_fix` listed with `None` and the reason `re-applies the bundled engine; takes nothing`.
- [ ] Run, expect FAIL.
- [ ] Implement. `share_engine_session`: `engine_view::read()`, `own_claude_conversation`, else `Err`; `sharing_on` false → `Err("sharing is off on this Mac")`; native confirm via the existing `confirm_native` helper in `lib.rs:886` area, copy: "Share this session with your workspace?" / "Teammates will be able to watch it. You can stop sharing at any time."; then `ask_share(key, true)` with `key` = `native_key` of provider `claude`, `profile` = `fingerprint(home/.claude/projects)` (same as `claude::capture`). `engine_fix`: `spawn_blocking` around `fix_at`.
- [ ] Run the focused tests and `cargo test --test invoke_surface`. PASS. Commit `desktop: Share a session from its engine row, Fix the background service`.

### Task 5: The page reads the engine

**Files:** Create `apps/frontend/src/components/messaging/engineDirectory.ts`. Modify `useSessionRows.ts`, `packages/shared/src/messaging/sessionRow.ts` + test, `localTerminal.ts` only if a helper is needed. Test: `apps/frontend/src/components/messaging/__tests__/engineDirectory.test.ts`.

**Interfaces, produced:**
```ts
export interface EngineDirectory { records: readonly EngineRecord[]; down: boolean }
export function useEngineDirectory(): EngineDirectory; // useSyncExternalStore over one poller, 5 s while subscribed
```
`useSessionRows`'s signature and return are unchanged. `EngineRecord` gains `own?: boolean`.

- [ ] Failing tests:
  - no bridge, or `hasEngineAsk` false → `{records: [], down: false}` and no `engine_ask("directory")` call.
  - a reject → `{records: [], down: true}`; the next good answer clears it.
  - records lacking `wakes` (a 0.7.x engine) → `{records: [], down: false}`.
  - an `{ok:false}` answer → `down: true`.
  - `mergeSessionRows`: `own: true` makes `isViewer` true with no `user_id`; a network-only ended row with reason `moved-on` is dropped; a joined one is kept; an engine record whose title, folder and agent equal an unjoined backend row (one with no `networkSessionId`) is dropped; with a `networkSessionId` backend row it is not treated as a twin; a joined ended engine row over a live backend row reads `ended`; `waiting_for` and `done` as the foundation tests pin.
- [ ] Run, expect FAIL.
- [ ] Implement. Mark the twin rule `// ponytail: a Mac that has not updated sends no networkSessionId; delete once all have.`
- [ ] Run `vitest run` on the two test files and typecheck the frontend (`pnpm --filter <frontend> exec tsc --noEmit`). PASS. Commit `sessions: the page reads the engine's directory`.

### Task 6: Today's layout shows them; the footer shows the fault

**Files:** Create `apps/frontend/src/components/messaging/NetworkSessionRow.tsx`, `apps/frontend/src/components/desktop/EngineFault.tsx`. Modify `MessagingWorkspaceContext.tsx`, `RoomDirectory.tsx` (Sessions section only, smallest edit), `AllSessionsView.tsx` (one condition), `roomSections` (`SidebarSession.kept?`), `WorkspaceSidebar.tsx` (one element), `packages/shared/src/messaging/localSessionLabels.ts` (`sidebarSessionList`).

**Interfaces:** consumes `useEngineDirectory`; `share_engine_session`; `engine_fix`.

- [ ] Failing tests:
  - `NetworkSessionRow`: shows title (`engine.title`, else agent), person, folder or `on the network`, state; no button named Open, Join, Resume or End; own + live + `agent === "claude"` has a `Share` button that invokes `share_engine_session` with `{sessionId: row.id}`; a teammate's row has none; states read `offline`, `parked, wakes when messaged`, `ended`, `waiting for you`.
  - `EngineFault`: `down` shows `Lightbulb's background service is off` and a `Fix` button invoking `engine_fix`; not down shows nothing.
  - `sidebarSessionList`: an offline row older than 3 days with `kept: true` stays listed; without it, it is dropped as today.
- [ ] Run, expect FAIL.
- [ ] Implement. Rows: `sessionRows.filter(r => r.source === "network")` goes to `RoomDirectory` as `networkSessions`. In the Sessions section they render after the shared rows in the same `<ul>`, filling the cap: `sessionsExpanded ? all : all.slice(0, max(0, CAP - shown.length))`, and `+N more` counts the hidden ones. The empty state shows only when both are empty. `kept` is true for a row with both `backend` and `engine`; `RoomDirectory`'s `listed` and `AllSessionsView`'s filter honour it. Row dimmed with the existing `session-row--gone`. `EngineFault` sits beside `NodeStatusPill` in `WorkspaceSidebar`.
- [ ] Run the test files, the existing `SessionListRow`, `RoomDirectory`, `sidebarSessionList` tests, and typecheck. PASS. Commit `sessions: network-only rows and the service fault in today's layout`.

### Task 7: Verify for real and record

**Files:** Modify this plan's Verification section below. Add a fixture only if the real output differs.

- [ ] If `df` shows at least 8 GB free: build `origin/feat/one-session-list` in a Linux container per `lightbulb-cli-e2e-testing` memory (never run a second daemon on the Mac), start its daemon in the container, register a session through its hook, capture real `sessions` and `directory` answers, and run them through `parse_sessions` (a throwaway test reading the captured file) and `page_record`. Record what differs.
- [ ] Run a real debug `lightbulb-desktop`-side check only if it does not touch the real service; otherwise record why not.
- [ ] Run `claude agents --json` next to the installed engine to confirm field words used by `listing_row` are still Claude's.
- [ ] Record V3 as open. Commit.

## Verification

Run 30 Oct 2026 on this Mac against a real 0.8.0 engine, a debug build of CLI origin/main (cd365ad) with its own home and identity at `/tmp/lb3`, reading the real `~/.claude` read-only. The Linux container steps were replaced by this. Rerun, with the engine up per the task: `LIGHTBULB_CONNECT_HOME=/tmp/lb3 cargo test --lib real_engine_drives_mirrors -- --ignored --nocapture`. Output: `.superpowers/sdd/2026-10-30-one-session-list-plan-3-desktop/task-7-report.md`.

**Bug found and fixed: the branch read no interactive session from a real engine.** `parse_sessions` (`engine_view.rs:74`, since ee6460385) had `job: Some((text(&r["job"], "id")?, ...)).filter(...)`. The `?` returned from the row's closure, so a row with no `job` was dropped, and no real interactive row has one. Before the fix, at 5cffda6fc, the test failed at the parse: one answer with 5 rows parsed to 0, and `discover` found nothing. `parses_and_skips` now has a row with no `job` and one with half a `job`; both parse with `job: None`. The remaining `?` in that closure are on `key`, `provider`, `conversation` and `pid`, which a row cannot do without.

Verified on the branch itself, after the fix, in two consecutive runs (`1 passed`, about 33 s each):
- `sessions` answered in 30 to 41 ms (limit 5 s). `discover` made 5 sessions from 5 live rows.
- A5 at one moment, on 5 real sessions: mirror, engine and `claude agents --json` agreed (3 busy, 2 idle). The engine's listing read did not time out.
- Every mirror's `network_id` is its engine row's `key`, and its `registration` frame carries it with the feature on.
- The end path on real answers, with `sleep` processes registered through the socket as provider `claude`. The real `discover` made both mirrors. A `waiting` row read `waiting-approval`; after the kill the real ended row said `process-exited`, the first miss ended nothing, the second ended the mirror as `process-exited`. An `idle` row Claude's listing does not name was ended by the engine as `moved-on` with its process alive (after its 15 s quiet); first miss nothing, second ended as `exited`. The kill was run on a `waiting` row because that row stays until its process exits, whatever the listing says.

Verified earlier in a throwaway copy of 5cffda6fc, and not rerun on the branch. The copy's one code change was the parse line above, which the page path does not use:
- Page side: 13 real `directory` records through `for_page` lost `cwd` and `resume`; no value contains `/Users` or `cwd`, and "resume" appears only as the key `can_resume`. `mergeSessionRows` with no backend rows gave 12 rows, all `source: network`, `isViewer: true`, 4 live (3 `busy`, 1 `idle`) and 8 `ended`. The 13th is the `moved-on` row, which the merge leaves out by design. `source: shared` is not exercised on real data.

Verified on the installed Claude Code 2.1.314, `claude agents --json`: `kind`, `pid`, `sessionId`, `cwd`, `status`; `--all` adds `id` and `state`. These are the words `listing_row` uses. Seen values: `interactive`, `background`, `busy`, `idle`, `stopped`, `done`.

Not verified:
- Real rows with a `job`, or Claude's `waitingFor`: no live background job and no prompt existed on this Mac during the run. The `waiting` row above was registered by hand, so a real permission prompt reaching `waiting-approval` is not verified, nor are the `working` and `blocked` states.
- A real hook's `register`, a real Claude session starting or ending: none was started, because the running Lightbulb.app could share it.
- A debug `lightbulb-desktop`: not run. It would read and could set up the real service, and a real window would touch the real relay.
- The native Share dialog, the live relay and backend, and the sidebar in a real window.
- A5 as a standing guarantee (one moment only). When Claude's listing is absent, the test can only tell a timed-out read from the engine's log, which says so once per run of failures.
- A6 (shared session actions), A7 (no Windows or Friday peer on this isolated engine), A10.
- V3: open. It needs Aden's Mac and cannot be run from here.
