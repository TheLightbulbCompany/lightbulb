# Shared local sessions: hooks-first lifecycle and liveness

Date: 2026-09-14. Status: approved design, phase one. Delete this file when the
work ships.

## 1. Goal

Replace the discovery and liveness layer under shared Claude/Codex sessions so
that a session the owner is using shows as live within a second, a session
that ended shows as ended and leaves the live list, and an enrolled Mac stops
spending CPU on polling. Fix the launch/restore path forward at the same time.

Non-goals for this phase (recorded as phase two in memory): Claude app and
Codex Desktop sessions, a recap line or LLM rollup, a workspace-level Sessions
surface or "started sharing" room line, a Claude Code statusline notice.

## 2. What is wrong today

All paths are relative to `apps/desktop/src-tauri/src/local_sessions/` unless
noted.

- `transport.rs:274-280` runs discovery every 2 s: `claude agents --json`
  (0.75 s), then per live session two `lsof -p` calls (0.3 s each), `date`,
  and a 2 MiB transcript tail. The Claude `SessionStart` hook only sends one
  byte to `/private/tmp/lightbulb-sessions-<uid>/wake.sock` to skip the wait.
- `transport.rs:275` swallows a discovery failure into an empty list and
  `reconcile_sessions` (`local_sessions.rs:888`) then marks every session
  offline.
- `transport.rs:534-539` and `:596-597` drop a session from the registered set
  whenever its observed state changes, so a working session must re-register
  before it can heartbeat again while an idle or dead one renews reliably.
- Heartbeats are one RPC per session every 10 s, single-flight, in BTreeMap
  key order (`transport.rs:456-495`). Ended sessions are never removed
  anywhere: mirror files persist on disk (cap 512, `transport.rs:247`),
  are re-registered on every reconnect, and are heartbeated forever. The
  backend has no `ended` state, no timestamps, no TTL, and lists sessions in
  uuid5 order (`core/repositories/local_sessions_repo.py:40`).
- `local_sessions.rs:238` `authenticate` sets `paused = human !=
  Some(human_user_id)`, so a launch before the webview reports a user pauses
  sharing. The only re-arm is `local_sessions_auth` from `useDesktopAuth`,
  whose failure the frontend swallows. After the 0.4.118 auto-update restart
  on this Mac, that raced pairing and mirroring never resumed.
- The `lightbulb-session-sharing` skill installs only on the enable action
  (`enrollment.rs:519`), never on restore.

## 3. Design

### 3.1 Desktop lifecycle (hooks first, sweep as backstop)

**Hook datagram.** `lightbulb-desktop local-session-hook` reads the hook JSON
Claude writes to its stdin and sends one datagram to the existing wake socket:

```json
{"v":1,"event":"SessionStart","sessionId":"…","transcriptPath":"…","cwd":"…","notificationType":"…"}
```

`event` is the hook's `hook_event_name`; `notificationType` is present only for
`Notification`. The payload is capped at 1500 bytes; if it does not fit, drop
`cwd`, then `transcriptPath` (the desktop resolves both from `claude agents
--json` on start). A bare `1` or unparsable datagram is treated as a wake
(today's behaviour). Datagrams are hints, never authority: nothing in a
datagram is trusted until the desktop has verified the process the same way
it does today (`claude.rs:23-119`).

**Installed Claude hooks.** `configure_hooks` (`claude.rs:678`) installs the
same command under `SessionStart`, `UserPromptSubmit`, `Stop`, `Notification`,
and `SessionEnd`, removal-exact per event as today, still refusing when
`disableAllHooks` is set. The backup file behaviour is unchanged.

**Event handling on the host:**

| event | action |
|---|---|
| `SessionStart`, or any event for an unknown session id | run `claude agents --json` once (throttled to one run per second host-wide), `capture` the matching row, add the mirror, register |
| `UserPromptSubmit` | status `busy` |
| `Stop` | status `idle` |
| `Notification` with `notificationType` `permission_prompt` | status `waiting-approval` |
| `Notification` with `notificationType` `idle_prompt` | status `idle`; other types ignored |
| `SessionEnd` | ended, reason `exited` |

Events other than start apply only to a mirror whose pid is alive. The hook
copies Claude's `notification_type` field when present; when it is absent, a
`Notification` whose message contains "permission" counts as
`permission_prompt` and any other is ignored. The first plan task verifies the
exact hook JSON of the installed Claude Code against this table.

**Liveness.** The relay loop's 100 ms tick (`transport.rs:412`) calls
`libc::kill(pid, 0)` for every non-ended mirror; `ESRCH` ends the session with
reason `process-exited`. The existing `process()` identity check stays on the
send path (`verified`, `transport.rs:669`).

**Sweep.** Discovery runs every 60 s and on wake instead of every 2 s. It
only adds sessions it did not know and refreshes the listing status of known
ones; when the listing disagrees with the hook-derived status, the listing
wins, because a hook can be missed. It never marks a session offline.
`reconcile_sessions` loses its all-offline loop. A failed listing logs once
per distinct error string and keeps last known state.

**Codex (spike resolved 2026-09-14).** Codex 0.149 loads a Claude-shaped
`~/.codex/hooks.json` and fires `SessionStart`, `UserPromptSubmit`, `Stop`, and
`SessionEnd` with `session_id` (the thread id), `transcript_path` (the rollout),
`cwd`, and `last_assistant_message` on Stop. An untrusted hook is skipped
silently; trust is a `[hooks.state."<abs hooks.json path>:<event_snake>:<group>:<handler>"]
trusted_hash = "sha256:…"` entry in `~/.codex/config.toml`, hashed over the
canonical JSON of the normalized hook definition. The desktop writes those
entries itself (recipe and live fixtures in the plan), so Codex follows the
same event table as Claude and there is no poll fallback. The hook command
carries the provider as its argument (`local-session-hook claude|codex`).

**Recap seed.** The Stop hook's `last_assistant_message` (first 300 chars)
rides the datagram, the mirror, the heartbeat, META, and the public JSON as
`lastAssistantMessage`. No UI reads it in this phase; it exists so the phase-two
recap line needs no second protocol change and no LLM call.

### 3.2 Wire protocol

Registration (`local.session.register`) is unchanged, including cursor
negotiation and the generation/successor logic for a resumed conversation.

`local.session.heartbeat` is removed. Its replacement is one frame per device:

```json
{"type":"local.device.heartbeat","sessions":[
  {"sessionId":"…","generation":"…","status":"busy","activeTurnId":null,
   "controlUnavailableReason":null,"title":"…","cwdName":"…",
   "lastAssistantMessage":null,"workspace":{…}}
]}
```

The four frames (heartbeat, its ack, ended, its ack) are golden JSON fixtures
under `packages/shared/fixtures/local-session-frames/`, asserted by both
`cargo test` and `pytest`, because Rust-versus-Python contract skew has already
caused one production outage.

Sent every 10 s, and early on any status, active-turn, or title change,
throttled to one per second. It lists every registered, non-ended mirror. The
ack is `{"type":"local.device.heartbeat.ack","rejected":["sessionId",…]}`; a
rejected session is unknown or ended on the server, and the desktop
re-registers it. This replaces the changed-means-re-register rule, which is
deleted along with the single-flight heartbeat selection.

New frame:

```json
{"type":"local.session.ended","sessionId":"…","generation":"…","reason":"exited","endedAt":1789400000}
```

`reason` is `exited` or `process-exited`. The ack is idempotent. After the ack
the desktop stops tailing and stops listing the session in heartbeats.

The desktop never sends the status `offline`. Offline is only the backend's
verdict when a lease lapses, meaning the device stopped talking.

No compatibility period: the backend accepts only the new frames once
promoted, and the desktop auto-updates. An old desktop's per-session heartbeat
is rejected as an unknown frame until it updates.

### 3.3 Backend data and listing

`core/services/local_session_relay.py` handles the two new frames. The device
heartbeat verifies every listed session belongs to the caller's owner and
device, renews `onlineUntil = now + LIVENESS_SECONDS` for each, stores the
status fields, and sets `lastEventAt` when status, active turn, or title
changed. Sessions that fail ownership or are ended come back in `rejected`.
`local.session.ended` sets `status = ended`, `endedAt`, `lastEventAt`.

META gains `lastEventAt` and `endedAt` (epoch seconds), set by register, the
events batch handler, a status-changing heartbeat, and ended. Read-side status
(`core/services/local_sessions.py:470`): `ended` if `endedAt` is set, else
`offline` if `onlineUntil` lapsed, else the stored status. The public JSON adds
`lastEventAt` and `endedAt` as ISO strings.

`list_resources` sorts sessions by `lastEventAt` descending and excludes ended
sessions unless the request passes `includeEnded=true`, in which case the 20
most recently ended are appended. Rows are never deleted. Command admission
(`local_sessions_repo.py:216`) additionally requires `attribute_not_exists(endedAt)`.

The `room.resources.changed` hint keeps firing on register, status change, and
ended.

### 3.4 Clients

`packages/shared/src/messaging/` types: `LocalSession.status` gains `"ended"`;
`lastEventAt?: string` and `endedAt?: string` are added; the label map gains
`ended: "Ended"`. `useRoomResources` takes `includeEnded` and passes it
through. Web `RoomResources.tsx` and mobile `room/[roomId]/devices.tsx` show a
relative last-activity time on each row and a "Show recent (N)" toggle that
refetches with `includeEnded`. Server order is preserved. No other UI changes.

### 3.5 Launch and restore

- `Enrollment::authenticate` takes the controller's `Option<Option<String>>`:
  unreported (`None`) leaves `paused` unchanged; signed out (`Some(None)`)
  pauses; a user id pauses only when it differs from `human_user_id`.
- The webview pairing completion path in `lib.rs` calls `Controller::restore`
  after `pair_state` is persisted.
- `local_sessions_auth` logs a failing restore via `crate::log` with the error;
  the frontend `useDesktopAuth` catch logs a `console.warn` instead of nothing.
- `restore` calls `install_skill` after a successful `prepare`; failure
  degrades to the status string exactly as enable does.
- Mirrors restored from disk start as `unknown`, not `offline`; the first tick
  ends any whose pid is dead. Mirrors already marked ended are loaded only so
  their files can be reaped; they are never registered again.

### 3.6 Reaping

A mirror carries `endedAt` once ended. Restore and the 60 s sweep delete mirror
files whose `endedAt` is older than 24 hours. Registration, heartbeats, and
the 512 cap consider only non-ended mirrors.

## 4. Error handling

- Unparsable datagram: treated as a wake, counted in the relay log once per
  minute at most.
- Listing failure during a sweep or a start event: log once per distinct
  error, keep state, retry on the next sweep or wake.
- Heartbeat `rejected`: re-register that session; if registration is rejected
  too, mark the mirror `unknown` and retry on the next sweep.
- Ended for a session the server does not know: ack anyway.
- Hook install failure on enable: unchanged (status string, enable proceeds).

## 5. Testing

- Rust (`cargo test local_sessions`): datagram parse and size fallback; every
  row of the event table; pid death ends within one tick; sweep failure keeps
  state and never sets offline; device heartbeat composition, early send and
  throttle; ended mirrors are excluded from heartbeats and the cap; reaping
  after 24 h; `authenticate` truth table; restore installs the skill.
- Backend (`uv run pytest tests -k local_session`): device heartbeat renews
  every listed session and rejects a foreign one; ended precedence over the
  lease; `lastEventAt` ordering; hiding and the 20-row cap; command admission
  rejects ended; unknown `local.session.heartbeat` is rejected.
- Clients (`pnpm test` in `apps/frontend`, `apps/mobile` run alone with a 600 s
  timeout): label map, toggle refetch, relative time.
- Live check on the owner's Mac after the desktop build: start a `claude`
  session, it is busy in the room within a second; end it, it reads ended and
  leaves the live list; the 22 stale sessions on this Mac end on first restore.

## 6. Rollout

Three PRs, reviewed and merged in order: backend, then shared plus clients,
then desktop. Merging ships dev backend and the mobile OTA automatically; the
client change is additive and safe against the old backend. Then, with the
deploy queue reading 0: prod promotion (`gh workflow run deploy.yml --ref
main`), then the prod desktop DMG dispatch. No dev soak; the owner ruled the
feature not sensitive.

## 7. Phase two (deferred, kept in memory)

Claude app and Codex Desktop session discovery; recap line and Haiku rollup;
sidebar Sessions surface and "started sharing" room line; Claude Code
statusline notice.

Ambient awareness for company agents (owner ruling 2026-09-14): the room
ingress payload carries nothing about members' shared sessions today, so an
agent plans its own fix and collides with a human's live Claude Code or Codex
session unless the human mentions it. Phase two injects one line per LIVE
session (human handle, provider, cwdName, title, status, sessionId,
latestEventSeq) into the room context at ingress next to `participants`, and
adds a `lightbulb` SKILL.md routing row telling agents to `read` the
`lightbulb_local_sessions` tool from `latestEventSeq - N` when a member's
session is live. The SKILL.md text change means a new extended image, so it
rides the OpenClaw 2026.9.4 fork rebase promotion (fork branch
`isol8/2026.9.4-rebase`), not a separate 9.3 image build.
