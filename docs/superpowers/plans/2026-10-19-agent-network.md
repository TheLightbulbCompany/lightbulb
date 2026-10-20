# Agent Network Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any workspace member's Claude Code or Codex session on any Mac, with or without the desktop window, appears in the sidebar under its owner's face and can message any other member's session; a sender the owner has not allowed is held until the owner allows them.

**Architecture:** The server stays the hub. The existing `lightbulb-desktop` binary gains `serve` (the local-sessions service with no window) and `join` (browser approval that pairs the Mac server-side). The backend reuses the session command queue, the offline inbox and the Ask-to-join rail; it adds a hold for unallowed session senders, a device-login handshake, a loop limit and a sender label.

**Tech Stack:** Python 3.12 / FastAPI / DynamoDB (`apps/backend`), Next.js 16 / React 19 (`apps/frontend`), Rust / Tauri 2 crate run headless (`apps/desktop/src-tauri`).

**Spec:** `docs/superpowers/specs/2026-10-19-agent-network-design.md`

## Global Constraints

- Server is the hub; no peer-to-peer; nothing polls on a timer (the CLI's login poll is the one exception: a user-facing wait).
- macOS only in v1.
- No backward compatibility (owner, 2026-10-19): there are no older apps; fix forward. Ship backend first, then the desktop release.
- Allowing a person allows all of that person's agents. Allow lists stay keyed on people (`allowedSenders`).
- A person's own agents reach each other instantly.
- Today's limits stand: offline TTL `OFFLINE_SEND_SECONDS` (24 h), `OFFLINE_SEND_PENDING_LIMIT` (20), ask expiry `ASK_TTL_S` (24 h).
- Loop limit: 12 messages per (source session, target session) per 600 s, beside today's `native_hop` cap.
- `lightbulb setup` sets `crossSessionInbound: "accept"` in `~/.claude/settings.json` (owner OK, 2026-10-19).
- `join` turns on the existing per-Mac "Share every new session" switch (`shell_integration::set(home, true)`).
- Sidebar rows show the owner's avatar (same `ParticipantAvatar` messages use) with the tool icon as a corner badge. No "doing" line.
- Backend tests: `cd apps/backend && uv run pytest <path> -q --no-cov`. Desktop tests: `cd apps/desktop/src-tauri && TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --lib <filter>`. Frontend: `cd apps/frontend && pnpm vitest run <path>`.

## Review Focus

1. A held message whose sender is later **declined** must never be delivered, and must read `rejected` on its receipt.
2. A held message whose sender is **removed from the workspace** before the owner allows them must not be delivered on a later Allow of someone else.
3. Two concurrent Allows (or Allow racing the 24 h expiry) deliver each held command exactly once.
4. A device-login code is single-use, expires (10 min), and an approval by a person in workspace A cannot pair into workspace B.
5. `serve` started twice on one Mac (launchd plus a manual run) must not run two services on the same sockets.

Each is pinned by a test in the owning task below.

---

## Phase 1: Claude Code and Codex, app-less

### Task 1: Hold an unallowed session sender until the owner allows them

**Files:**
- Modify: `apps/backend/core/services/native_session_tools.py` (`_session_send` ~:376, `session_send` ~:417)
- Modify: `apps/backend/core/services/local_sessions.py` (`submit_command` :1702, `decide_join` :2760, `CommandReceipt.state` :614)
- Modify: `apps/backend/core/services/hosted_mcp.py` (`send_message` session branch)
- Modify: `apps/backend/core/services/local_session_relay.py` (`dispatch_session`: skip held commands)
- Test: `apps/backend/tests/unit/services/test_session_sender_hold.py`

**Interfaces:**
- Produces: `submit_command(..., hold_for: str | None = None)`. A command stored with `heldFor = <sender user id>` reads state `awaiting_approval`, and `dispatch_session` never sends it while `heldFor` is set.
- Produces: `async def release_held(owner: str, session_id: str, user_id: str, decision: Literal["allow","decline"]) -> int` in `local_sessions.py`. It returns the number of commands released or rejected.

- [ ] **Step 1: Write the failing tests**

```python
async def test_unallowed_session_sender_is_held_and_files_ask_to_join(...):
    receipt = await native_session_tools.session_send(aden_auth, my_target, "hi", rid, **aden_source)
    assert receipt["state"] == "awaiting_approval"
    assert my_row_after["joinRequests"][ADEN]["state"] == "pending"
    assert relay_dispatched == []          # nothing reached my Mac

async def test_allow_releases_held_commands_once(...):
    await local_sessions.decide_join(me, sid, ADEN, "allow")
    await local_sessions.decide_join(me, sid, ADEN, "allow")   # repeat is a no-op
    assert [c["state"] for c in commands_from(ADEN)] == ["pending"] and dispatched_count == 1

async def test_decline_rejects_held_and_never_delivers(...):
    await local_sessions.decide_join(me, sid, ADEN, "decline")
    assert command_state(rid) == "rejected" and relay_dispatched == []

async def test_removed_member_held_command_is_not_released(...):
    deactivate_stream(ADEN); await local_sessions.decide_join(me, sid, ADEN, "allow")
    assert command_state(rid) == "rejected"

async def test_own_agents_and_allowed_people_are_not_held(...):
    # owner's other session, and a person already in allowedSenders, go straight to "pending"

async def test_hosted_mcp_session_send_from_unallowed_person_is_held(...):
```

- [ ] **Step 2: Run them; expect FAIL** (`LOCAL_SESSION_FORBIDDEN` today)

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_sender_hold.py -q --no-cov`

- [ ] **Step 3: Implement**

The holding path covers both `_session_send` and hosted `send_message`. Call `authorize_session(auth, None, sid)` (read) instead of `owner_only=True`. If `not local_sessions.may_control(target, auth.user_id)`:
1. Call `request_join(auth, sid)`.
2. Submit with `hold_for=auth.user_id`, using `deliverBy = now + ASK_TTL_S`.

`release_held` runs from `decide_join` after the write lands:
- On allow, it re-checks that the sender's user stream is active, then clears `heldFor` (conditional on it still being set) and calls `dispatch_session`.
- On decline or an inactive stream, it sets `state="rejected"`.

The conditional clear is what keeps a racing Allow from delivering twice.

- [ ] **Step 4: Run tests; expect PASS.** Also run `tests/unit/services/test_native_session_tools.py tests/unit/services/test_hosted_mcp_auth.py tests/unit/services/test_hosted_mcp_session_caller.py`.

- [ ] **Step 5: Commit** `feat(sessions): hold an unallowed session sender until the owner allows them`

### Task 2: Offline inbox for session senders, the loop limit, and a delivery word on receipts

**Files:**
- Modify: `apps/backend/core/services/local_sessions.py` (`offline_send` predicate ~:1828)
- Modify: `apps/backend/core/services/native_session_tools.py` (`session_send` return)
- Test: `apps/backend/tests/unit/services/test_session_send_delivery.py`

**Interfaces:**
- Produces: `session_send(...)` returns `{"state", "destination", "targetRequestId", "delivery"}`, where `delivery` is one of `"now"`, `"queued"`, `"waiting for owner"`.
- Produces: the error `ApiProblem("LOCAL_SESSION_LOOP", "Too many messages to this session; wait a few minutes.")`.

- [ ] **Step 1: Failing tests**

```python
async def test_session_sender_to_offline_mac_is_queued_not_refused(...):
    assert receipt["state"] == "pending" and receipt["delivery"] == "queued"

async def test_hosted_mcp_send_to_offline_session_is_queued(...):

async def test_thirteenth_message_in_ten_minutes_is_refused(...):
    for i in range(12): await send(i)
    with pytest.raises(ApiProblem, match="LOCAL_SESSION_LOOP"): await send(12)

async def test_loop_window_reopens_after_600s(...):

async def test_delivery_word(...):
    assert live_claude["delivery"] == "now" and held["delivery"] == "waiting for owner"
```

- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
  - Drop `not source_session_id` and `not via_client` from `offline_send`. Keep every other clause.
  - The loop check lives in `session_send`. It counts this target's commands with the same `sourceSessionId` and a `createdAt` within the last 600 s, before submitting.
  - `delivery` is derived from the receipt state plus the target's live status.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat(sessions): session senders use the offline inbox; loop limit; delivery word`

### Task 3: Name the sending agent ("Aden's Codex")

**Files:**
- Modify: `apps/backend/core/services/local_session_relay.py` (`_sender_label` :293)
- Test: `apps/backend/tests/unit/services/test_local_session_relay_sender_label.py`

**Interfaces:**
- Produces: for a session actor, `_sender_label` reads `"<Owner first name>'s <Claude Code|Codex>"`. The provider comes from the source session row; the owner name comes from `messaging.human_display_name`.

- [ ] **Step 1: Failing test** — `assert label == "Aden's Codex"` for a command whose actor is a Codex session owned by Aden; the owner's own session reads `"Your Claude Code"`.
- [ ] **Step 2: Run; FAIL** (today: "Shared native session").
- [ ] **Step 3: Implement** in `_sender_label`'s session branch.
- [ ] **Step 4: PASS.**
- [ ] **Step 5: Commit** `feat(sessions): a session sender is named as its owner's agent`

### Task 4: Device login, where the browser approval pairs the Mac

**Files:**
- Modify: `apps/backend/routers/desktop_auth.py`
- Create: `apps/backend/core/services/device_login.py`
- Create: `apps/frontend/src/app/auth/connect-machine/page.tsx` (copy the shell of `app/auth/desktop-callback/page.tsx`)
- Test: `apps/backend/tests/unit/routers/test_device_login.py`

**Interfaces:**
- `POST /api/v1/auth/device/start` takes `{tauriMacPubKey, cliPubKey}` with no auth. It returns `{code, userCode, verifyUrl, interval: 2, expiresIn: 600}`.
  - `code` is 32 random url-safe bytes. `userCode` is 8 characters `XXXX-XXXX`.
  - The row is `DEVICELOGIN#<sha256(code)>` in the messaging table, with `ttl = now + 600`.
- `POST /api/v1/auth/device/approve` takes `{userCode}` with Clerk auth. It requires an org (`resolve_owner_id`), then runs, server-side:
  1. `pair_desktop_devices` (as `post_pair` does), with `permissions` `{screen:false, shell:false, browser:false, files:false}`.
  2. `local_sessions.set_enrollment(ctx, EnrollmentRequest(device_id=mac, cli_device_id=cli, enabled=True))`.
  3. It stores the `PairResponse` plus `{humanUserId, enrollment}` on the row as `approved`.
- `POST /api/v1/auth/device/poll` takes `{code}` with no auth. It returns `{status: "pending"|"approved"|"expired"}`, plus the stored result once, on the first `approved` read. It then deletes the row.
- `verifyUrl` is `https://<frontend>/auth/connect-machine?code=<userCode>`. The page shows Clerk sign-in when signed out, then one **Connect this Mac** button that calls approve, then "Connected. You can close this tab."

- [ ] **Step 1: Failing tests**

```python
def test_start_then_poll_is_pending(...):
def test_approve_pairs_and_enrolls_then_poll_returns_once(...):
    assert first["status"] == "approved" and first["ownerId"] == ORG_A
    assert second["status"] == "expired"           # single use
def test_code_expires_after_600s(...):
def test_approve_uses_the_approvers_own_workspace(...):  # Review Focus 4
def test_approve_without_org_is_409_workspace_required(...):
```

- [ ] **Step 2: Run; FAIL.**
- [ ] **Step 3: Implement** the service, then the three routes. Run `/auth/connect-machine` through Clerk middleware like `/settings`. Add the frontend page.
- [ ] **Step 4: Run backend tests; PASS.** Then `cd apps/frontend && pnpm lint && pnpm vitest run src/app/auth` (the page renders signed-out and signed-in states).
- [ ] **Step 5: Commit** `feat(auth): device login pairs a Mac from the browser`

### Task 5: Owner's face on every session row

**Files:**
- Modify: `apps/frontend/src/components/messaging/roomSections.ts` (`SidebarSession` :55)
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (`sidebarSessions` ~:383)
- Modify: `apps/frontend/src/components/messaging/SessionListRow.tsx`
- Test: `apps/frontend/src/components/messaging/__tests__/SessionListRow.test.tsx`

**Interfaces:**
- `SidebarSession` gains `owner: RoomParticipant`. It is a human participant built from the roster member (`userId`, name, image), the same shape `ParticipantAvatar` takes.

- [ ] **Step 1: Failing test** — a teammate's row renders `ParticipantAvatar` for the owner, with the tool icon inside a `.session-tool-badge--corner` element; the accessible label still names the owner.
- [ ] **Step 2: Run; FAIL.**
- [ ] **Step 3: Implement.**
  - Render `<ParticipantAvatar participant={owner} size="sm" showPresence={false} />`, with `SessionToolIcon` as an absolutely positioned corner badge, on every row (own rows too).
  - Add the CSS in `messaging.css` using existing tokens only. `brandTokens.test.ts` must still pass.
- [ ] **Step 4: Run `pnpm vitest run src/components/messaging src/__tests__/brandTokens.test.ts`; PASS.**
- [ ] **Step 5: Commit** `feat(web): a session row shows its owner's face`

### Task 6: `serve`, the local-sessions service with no window

**Files:**
- Modify: `apps/desktop/src-tauri/src/main.rs` (arg dispatch :5-47)
- Modify: `apps/desktop/src-tauri/src/local_sessions/enrollment.rs` (`state_dir` :506, `sharing_on` :480, `enrolled_owner` :486, `launch_scope` :494, `session_scope` :503, `Controller::prepare` :247, `restore` :387)
- Create: `apps/desktop/src-tauri/src/headless.rs`
- Test: in `headless.rs` `#[cfg(test)]`

**Interfaces:**
- `pub fn app_data_dir() -> PathBuf` returns `~/Library/Application Support/company.thelightbulb.desktop`. It is the same path Tauri resolves for the identifier.
- Each enrollment helper above takes `state_dir: &Path` in place of `&AppHandle`. The Tauri callers pass `state_dir(app)` (one wrapper stays in `enrollment.rs`).
- `pub fn serve() -> !` in `headless.rs`:
  - Builds a tokio runtime.
  - Takes an exclusive `flock` on `<app_data_dir>/serve.lock`, and exits 0 with "already running" if it is held (Review Focus 5).
  - Loads `pair_state` and `enrollment.json`, then runs `Controller::restore(state_dir, …)` and the same spawns `prepare` does.
  - Runs until SIGTERM.
- `lightbulb-desktop serve` dispatches to it.
- `pub fn install_launch_agent(exe: &Path) -> io::Result<()>` writes `~/Library/LaunchAgents/company.thelightbulb.serve.plist` (`ProgramArguments [exe, "serve"]`, `RunAtLoad`, `KeepAlive`) and runs `launchctl bootstrap gui/<uid>`.
- The desktop app does not start a second service when it sees the lock held. The window becomes a viewer of the running service; check this in `lib.rs:2040` `restore`.

- [ ] **Step 1: Failing tests** — `second_serve_exits_when_lock_held`; `plist_runs_serve_with_keepalive` (asserts the generated XML); `state_dir_helpers_read_enrollment_without_tauri` (writes an `enrollment.json` to a temp dir, asserts `sharing_on(dir)` and `enrolled_owner(dir)`).
- [ ] **Step 2: Run** `cargo test --lib headless`; FAIL.
- [ ] **Step 3: Implement** the refactor (signature changes only, no behavior change), then `headless.rs` and the `main.rs` arm.
- [ ] **Step 4: Run** `cargo test --lib local_sessions` and `cargo test --lib headless`; all PASS.
- [ ] **Step 5: Commit** `feat(desktop): lightbulb-desktop serve runs local sessions with no window`

### Task 7: `lightbulb join`, `setup` and `share`

**Files:**
- Modify: `apps/desktop/src-tauri/src/cli.rs` (`main` :526, `install` :581)
- Modify: `apps/desktop/src-tauri/src/headless.rs`
- Modify: `apps/desktop/src-tauri/src/local_sessions/claude.rs` (`configure_hooks` :1323)
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (`Command::header` :659 / `native_text`)
- Test: `headless.rs` and `claude.rs` `#[cfg(test)]`

**Interfaces:**
- `lightbulb join`:
  1. `keychain::load_or_generate` gets the two keys.
  2. `POST /auth/device/start`. It prints `Open <verifyUrl> and enter <userCode>` and runs `open <verifyUrl>`.
  3. It polls `/auth/device/poll` every `interval` seconds for at most `expiresIn`.
  4. It writes `pair_state` and `enrollment.json` from the result.
  5. It runs `reassert(state_dir)` (hooks, MCP entry, skill), then `shell_integration::set(home, true)`, `cli::install` (the `~/.local/bin/lightbulb` link, now callable without the GUI), and `install_launch_agent`.
  6. It prints `Connected as <name> in <workspace>.`
- `lightbulb share on|off` calls `shell_integration::set`.
- `configure_hooks` also sets `crossSessionInbound` to `"accept"` in `~/.claude/settings.json`, preserving every other key.
- `defang(body: &str) -> String` replaces `[` with `［` (U+FF3B) at the start of any line that starts with the header prefix or a confusable of it (NFKC-fold, then compare). The header is built from the defanged body.

- [ ] **Step 1: Failing tests:**
  - `join_writes_state_from_poll_result` (a mocked HTTP server returns pending, then approved).
  - `join_times_out_after_expires_in`.
  - `configure_hooks_sets_cross_session_inbound_accept_and_keeps_other_keys`.
  - `defang_neutralises_forged_header_and_lookalikes`: a body line `[Lightbulb teammate contribution from @ceo …]` and a fullwidth-letter lookalike are both rewritten; plain text is unchanged.
- [ ] **Step 2: Run; FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run `cargo test --lib`; PASS.**
- [ ] **Step 5: Commit** `feat(desktop): lightbulb join pairs this Mac from the browser and starts the service`

### Task 8: Ship and verify phase 1

- [ ] Open one PR per task group (backend 1-4, web 5, desktop 6-7). Merge on a Codex review.
- [ ] Dispatch `deploy.yml` (backend and web), then `desktop-build.yml`. Coordinate with the other sessions first; check that nothing is queued.
- [ ] On the owner's Mac, after the update:
  1. Quit the window app.
  2. `lightbulb join` and approve in the browser.
  3. `launchctl print gui/$UID/company.thelightbulb.serve` shows it running.
  4. A plain `claude` started in a terminal appears in the sidebar under the owner's face.
- [ ] From a second account's session (a dev test account), message the owner's session:
  1. Expect `awaiting_approval` and a Needs-you Ask to join.
  2. Allow it; the message lands in the session as "<Name>'s Claude Code".
  3. Send 13 in 10 minutes and expect `LOCAL_SESSION_LOOP`.
- [ ] Delete this plan's phase 1 section once shipped. Keep phases 2-3 until they ship.

## Phase 2: Cursor and Grok CLI (outline; plan when phase 1 ships)

- Widen `provider` to `"claude"|"codex"|"cursor"|"grok"` in the 7 places the backend map lists:
  - `local_sessions.py:127,269`, `local_session_relay.py:66`, `native_session_tools.py:33`
  - `packages/shared/src/messaging/types.ts:389,523,590`
  - icons in `SessionToolIcon.tsx`
- Hook files and outputs, ported from reason-network:
  - Cursor: `~/.cursor/hooks.json`; `additional_context`, and `followup_message` on stop.
  - Grok CLI: `~/.grok/hooks/lightbulb.json`. Only honor Stop with `reason == "end_turn"`. Ignore `subagentType`. Its SessionStart and prompt-submit output is dropped.
- MCP entries: `~/.cursor/mcp.json`, and `~/.grok/config.toml`'s `[mcp_servers.lightbulb]` plus `permission.allow`.
- Registration: these agents have no passive registry, so a session exists from its first hook. Their `delivery` word is `"next step"`.
- The unread note: every `lightbulb` tool result for a session caller carries `unread` when that session has undelivered commands.

## Phase 3: Grok Bot (outline; plan after verifying the webhook)

- Verify first: create a Grok Bot routine with a webhook trigger, POST to it, and confirm the bot receives the body. If the trigger is absent, ship send-only.
- An agent row kind `grok_bot`: owner, name, and a webhook URL and key stored via `secrets_manager.create_or_update_secret` under `lightbulb/{env}/agent-webhooks/{owner}/{agentId}`.
- A Settings section modelled on `routers/settings_keys.py`.
- `dispatch_session`'s equivalent for a `grok_bot` target is one `httpx.post(url, headers={"Authorization": f"Bearer {key}"}, json={...})`. Same hold, loop and label rules.
- Agent keys (`lbk_` prefix, hashed, a third branch in `LightbulbVerifier`): only if Grok Bot's OAuth sign-in to the hosted MCP fails.
