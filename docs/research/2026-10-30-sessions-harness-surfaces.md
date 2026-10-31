# Harness session surfaces: what a third party can see (30 Oct 2026)

Read-only research on this Mac. Engine = lightbulb-network `origin/main` (installed build `lightbulb 0.7.3`).
Desktop app = lightbulb.nosync `origin/main` `apps/desktop/src-tauri/src/local_sessions/`.
Tags: **C** = CONFIRMED (evidence given), **U** = UNCONFIRMED.

---

## 1. Claude Code CLI (installed 2.1.314)

### `claude agents --json` (the documented, supported surface)
- **C** Docs: "`claude agents --json` is the supported way to read session state from outside Claude Code… Poll `claude agents --json --all`… and read each entry's `state`, `status`, and `waitingFor`." (https://code.claude.com/docs/en/agent-view). Agent view as a whole is labelled **research preview**. `~/.claude/jobs/<id>/` is explicitly "not a stable interface".
- **C** Schema (docs table + binary `printAgentsJson`, 2.1.314 offset ~262747387):
  - always: `cwd`, `kind` (`interactive` | `background`), `startedAt` (ms)
  - background only: `id` (short id for `claude attach/logs/stop/respawn`), `state` = `working|blocked|done|failed|stopped`
  - while process alive: `pid`, `status` = `busy|waiting|idle`
  - `waitingFor` when `status=waiting`: `permission prompt`, `input needed`, `sandbox request`, `worker request`, `dialog open`
  - `sessionId` (full UUID, usable with `claude --resume`), `name`
- **C** Mapping in binary: `function p(i){return i==="idle"?"idle":i==="waiting"?"waiting":"busy"}` — so the pid-file status `shell` is reported as **busy**. Background `state`: `busy`→`working`; terminal job → `done|failed|stopped`; `tempo==="blocked"||status==="waiting"` → `blocked`; else `working`.
- **C** Rows with `parkedJobId` set are skipped (`if(t.parkedJobId!==void 0)continue`): a terminal parked into the agent view is shown only as its background job.
- **C** Live output on this Mac (5 rows): 4 terminal sessions + the desktop-app session `8f880960…` (pid 61408, `kind: interactive`, name "Polymarket trading bot research"). `--all` → 22 rows: 17 `background` (12 `stopped`, 5 `done`, no pid) + 5 interactive.
- **C** Cost: 0.2–0.25 s per call; did not start the supervisor (no supervisor process before or after).
- **C** Versions (`~/.claude/cache/changelog.md`): introduced **2.1.145** ("Added `claude agents --json` to list live Claude sessions as JSON for scripting"); `waitingFor` 2.1.162; `--all` + `id` + `state` 2.1.169 (also fixed omission of blocked background sessions); waiting-on-sandbox/MCP-input shown as Needs input 2.1.212; raw-mode fix 2.1.257. Agent view itself 2.1.139. (`claude agents` in 2.1.50 meant something else: "list all configured agents".) Lightbulb desktop's floor 2.1.145 is correct (`local_sessions/claude.rs:13-16`).
- **C** Docs say interactive sessions in other terminals "don't appear [in agent view] until you background them" — but `--json` does list them (observed). Documented ≠ observed here; treat the inclusion of interactive rows as observed behaviour.

### `~/.claude/sessions/<pid>.json` (per-process registry)
- **C** Observed fields: `pid, sessionId, cwd, startedAt, procStart (UTC string), version, peerProtocol, peerFeatures, kind, entrypoint (cli | claude-desktop), pidDomain, messagingSocketPath, name, nameSource, nameSince, updatedAt, status, statusUpdatedAt, asksForConvertedSkills, hostSessionId (desktop only)`. A `.key` file sits next to each (not read).
- **C** Parser in binary (offset ~228962481) also reads: `jobId, parkedJobId, spare, bridgeSessionId, logPath, waitingFor, agent, state, detail, tempo (active|idle|blocked), needs, tmux, formerNames, procStartFt`.
- **C** Enumerations in binary: `kind ∈ ["interactive","bg","daemon","daemon-worker"]`, `status ∈ ["busy","shell","idle","waiting"]`.
- **C** `shell` = turn idle **but a background Bash task is still running**: `cn = co==="idle" && Co ? "shell" : co`, `Co = some(task.type==="local_bash" && !finished)`.
- **C** `waiting` written from the SDK/permission path: `PV={running:"busy",requires_action:"waiting",idle:"idle"}`, `waitingFor` = `input needed` or `permission prompt`.
- **C** Files are removed on exit (all 5 files on disk have live pids). Undocumented format → **unstable** (fields grew across 2.1.2xx–2.1.3xx per desktop comments: `parkedJobId` 2.1.289, entrypoint values 2.1.304).

### Daemon / jobs
- **C** `~/.claude/daemon/roster.json` = `{proto, supervisorPid, updatedAt, workers:{}}`; currently empty, supervisor 92402 not running. Docs: "Running sessions, used to reconnect after a restart."
- **C** `~/.claude/jobs/<short>/state.json` keys: `state, detail, tempo, intent, name, nameSource, sessionId, resumeSessionId, daemonShort, cliVersion, cwd, backend, createdAt, updatedAt, firstTerminalAt, lastTerminalAt, linkScanPath…`. Docs: not stable; read via `claude agents --json`.
- **C** Supervisor reclaims an unattached finished/waiting session's process after ~1 h; the session stays listed (state) without pid. Machine shutdown → stopped/failed.

### Transcripts `~/.claude/projects/<slug>/<sessionId>.jsonl`
- **C** Row types seen (last 60 files): `ai-title {aiTitle}`, `custom-title {customTitle}`, `agent-name {agentName}`, `continued-in {continuedInSessionId}`, `relocated {relocatedCwd}`, `worktree-state`, `last-prompt`, `pr-link`, `bridge-session`, `cost-state`, plus user/assistant/system. Title rule used by desktop: last `custom-title`, else `ai-title` (claude.rs ~790). No "session ended" row exists; end is the pid file disappearing.
- **C** `continued-in` written when a conversation moves on under a new id (←, `/background`, peer reply) — 2.1.289+ per desktop comment.

### Resume
- **C** `claude --resume <sessionId>` (help); `--bg` with `--resume` continues it in the background; `claude attach <id|name>`, `claude respawn <id>` for background jobs (docs). Docs: "Two processes can't write to the same transcript."

---

## 2. Claude desktop app Code sessions

- **C** Same pid-file registry: `61408.json` has `entrypoint: "claude-desktop"`, `hostSessionId: "local_bae87a36…"`. Process: `~/Library/Application Support/Claude/claude-code/2.1.309/…/claude.app/Contents/MacOS/claude --output-format stream-json … --setting-sources=user,project,local --settings {fastMode, deniedMcpServers}` (no `hooks`/`disableAllHooks` override).
- **C** Appears in `claude agents --json` (row above).
- **C** Runs `~/.claude/settings.json` hooks: user setting source is loaded, and the engine store has a registered row for pid 61408 with handle `claude-104` and hook-resolved cwd `/Users/…/isol8.nosync/kalshi-bot/` (pid file says `/isol8.nosync`), executable `…/claude-code/2.1.309/…`.
- **C** Transcript is in the normal place: `~/.claude/projects/-Users-prasiddhaparthsarthy-Desktop-isol8-nosync/8f880960-….jsonl` → `claude --resume <id>` from a terminal should work. **U**: not run (would be a second writer while the desktop process is alive).
- **C** The app keeps its own registry: `~/Library/Application Support/Claude/claude-code-sessions/<acct>/<org>/local_<id>.json` with `cliSessionId`, `cwd`, `title`, `titleSource`, `isArchived`, `lastActivityAt`, `postTurnSummary.status_category`. Undocumented. This is the only place an *ended* desktop session's title/archived flag lives.
- **C** Status values: desktop writes `waiting` via the `requires_action` path; `waitingFor` names the prompt.

---

## 3. Codex CLI (`/opt/homebrew/bin/codex` 0.163.0) and the Codex app in ChatGPT.app (bundled `codex-cli 0.164.0-alpha.11.1`)

- **C** The ChatGPT app runs one `…/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex … app-server` process (pid 19747) for all its threads, plus an `exec-server --remote` process.
- **C** CLI has a shared local daemon: `codex app-server daemon {start,stop,version,…}`, `codex agents` ("Browse all agent sessions on the shared local app-server daemon", TUI only, no `--json`), `codex queue --thread --message`, `codex resume [SESSION_ID|name] [--last|--all]`, `codex fork`, `--no-daemon`. Daemon releases at `~/.codex/packages/app-server-daemon/releases/{0.163.0,0.164.0}-aarch64-apple-darwin`; not running right now.
- **C** App-server JSON-RPC (generated schema, `codex app-server generate-json-schema`): requests `thread/list`, `thread/loaded/list`, `thread/read`, `thread/resume`, `thread/name/set`…; notifications `thread/started`, `thread/status/changed`, `thread/closed`, `thread/name/updated`, `turn/started`, `turn/completed`. `ThreadStatus = notLoaded | idle | systemError | active{activeFlags: [waitingOnApproval|waitingOnUserInput]}`. `ThreadSourceKind = cli|vscode|exec|appServer|subAgent…`.
- **C** Docs (https://learn.chatgpt.com/docs/app-server): `thread/loaded/list` = "thread ids currently loaded in memory"; a thread stays loaded until no subscribers and no activity for 30 min, then `notLoaded` + `thread/closed`. "The app-server command and WebSocket transport are **experimental** and aren't supported for production workloads." Status is per app-server process: a separate client sees only the threads *that* process has loaded — the ChatGPT app's private app-server is not reachable (stdio). The shared daemon is reachable via its control socket (`/tmp/codex-daemon-<uid>/…`, per desktop codex.rs:124-138).
- **C** On-disk: `~/.codex/state_5.sqlite` table `threads` (id, rollout_path, cwd, title, name, source, originator, archived, created/updated_at_ms, first_user_message, git_branch, cli_version, history_mode…). No running/busy column. App threads are `source=vscode`, `originator` `Codex Desktop`/empty (VS Code extension writes the same `vscode`; desktop distinguishes by originator, codex.rs:6-55). `~/.codex/session_index.jsonl` {id, thread_name, updated_at}. Rollouts `~/.codex/sessions/YYYY/MM/DD/rollout-…-<id>.jsonl` with `session_meta`, `event_msg task_started/task_complete`. All undocumented, schema-migrated (state_**5**, history_mode legacy→paginated).
- **C** Hooks (https://learn.chatgpt.com/docs/hooks): `SessionStart, SessionEnd, SubagentStart/Stop, UserPromptSubmit, PreToolUse, PermissionRequest, PostToolUse, PreCompact, PostCompact, Stop, Interrupt`. `SessionEnd` "runs when an open conversation is archived or deleted, when Codex closes normally, or after 30 minutes idle with no connected client". Payload `session_id, transcript_path, cwd, hook_event_name, model`. Hooks need hash trust (engine writes it, setup/codex.rs). Enabled by default.
- **U** Whether hooks fire inside the ChatGPT app: docs are silent; `SessionEnd`'s "no connected client" wording implies the app-server path runs them. No evidence on this Mac: engine store's Codex rows start 29 Oct 22:22 and the last app thread was 27 Oct; zero app threads registered. Needs one live test.
- **C** Busy/idle via hooks: engine marks busy on UserPromptSubmit/PostToolUse, idle on Stop (hook.rs:179-182). No PermissionRequest hook installed → approval waits are invisible.
- **C** Process sharing: a TUI on the shared daemon and every ChatGPT-app thread register the *server's* pid (classify: file name `codex` or path `/app-server-daemon/` → codex, caller.rs:23-37). Pid liveness then says nothing about one thread; only SessionEnd ends it (up to 30 min late, never if the hook doesn't fire).
- **C** Resume: `codex resume <uuid|name>`; app-server `thread/resume`.

---

## 4. Cursor, OpenCode, Grok (brief)

**Cursor** (`cursor-agent 2026.10.22`)
- **C** Hooks (cursor.com/docs/hooks): `sessionStart/sessionEnd/beforeSubmitPrompt/stop/postToolUse/…`, fields `conversation_id`, `workspace_roots`, `transcript_path`. Docs don't say which hooks run in CLI vs editor (only `workspaceOpen` says "desktop app and CLI").
- **C** No machine-wide running list. `cursor-agent ls` (interactive picker), `--resume [chatId]`, `--continue`. Data `~/.cursor/chats/…`, `ide_state.json` (undocumented).
- **C** Engine misses **Cursor IDE** agent sessions: `identify_in` requires `classify(first program) == "cursor"` and classify only knows `cursor-agent` / `…/cursor-agent/…/node`; `Cursor.app` helpers only pass `is_cursor` (used to suppress Claude hooks), caller.rs:23-37,117-146. **U** that the IDE fires `~/.cursor/hooks.json` at all (likely; not tested).

**OpenCode** (1.18.32)
- **C** Every TUI starts its own server on a random port (opencode.ai/docs/server); `opencode serve` defaults to 127.0.0.1:4096. Endpoints `GET /session`, `GET /session/status` (map id→SessionStatus, values undocumented), SSE `/event`. No machine-wide discovery of which ports are live.
- **C** `opencode session list --format json [-n N]` lists stored sessions; data in `~/.local/share/opencode/opencode.db`. Resume `opencode -s <id>` / `-c`.
- **C** Engine uses a plugin (setup/opencode-plugin.js): `session.created`→start, `session.idle`→stop, `session.deleted`→end. TUI exit sends nothing; engine relies on pid liveness (fine, one process per TUI).

**Grok CLI** (1.0.67)
- **C** `~/.grok/active_sessions.json` (empty list now — likely the running registry, **U** schema), `~/.grok/leader.sock` + `grok leader list`, `grok sessions list|search` (no JSON flag seen), per-session `~/.grok/sessions/<cwd-enc>/<id>/summary.json` (`generated_title`, `cwd`, `last_active_at`, `last_turn_summary`) and `events.jsonl` (`turn_ended`). Resume `grok -r <id|title>`, `-c`.
- **C** Engine: hooks in `~/.grok/hooks/lightbulb.json` (setup/grok.rs).

---

## 5. How the engine discovers sessions today (origin/main)

- `sessions::all` (sessions.rs:243-267) = hook-registered store rows (`from_row`) + `scan_claude` pid files, dedup by key, **`Gone` dropped** (line 260), title/doing from transcript.
- `scan_claude` (sessions.rs:175-236): reads every `~/.claude/sessions/*.json`, checks pid start ±10 s; `status == "busy"` → `Live`, **everything else (`shell`, `waiting`, `idle`) → `Idle`**; ignores `kind`, `parkedJobId`, `waitingFor`, `entrypoint`.
- `from_row` (sessions.rs:128-170): alive iff pid+birth match; status from the last hook (`busy`→Live).
- Status enum has only `Live | Idle | Gone` (sessions.rs:59-65): no "waiting for you", no "ended/resumable".
- Hooks per harness: Claude `~/.claude/settings.json` (5 events), Codex `~/.codex/hooks.json` + trust hashes, Cursor `~/.cursor/hooks.json`, Grok `~/.grok/hooks/lightbulb.json`, OpenCode plugin. Events: SessionStart, UserPromptSubmit, PostToolUse, Stop, SessionEnd (hook.rs:33-63). No Notification/PermissionRequest hook.
- The store keeps `ended_at` per row (`~/.lightbulb/connect/state.db`), but nothing lists ended rows.

### What the engine is missing, per harness
| Harness | Missing today |
|---|---|
| Claude CLI | (1) `waiting` (permission prompt / question) shown as idle; (2) `shell` shown idle while Claude itself says busy — observed live: pids 85940 and 8252 are `busy` in `claude agents --json`, `idle` in `lightbulb who`; (3) background sessions with no process (agents `--json` lists `working/blocked/done`, engine nothing); (4) parked terminals (`parkedJobId`) still listed under the old conversation, which Claude skips; (5) no ended/resumable list; (6) `claude -p` runs (entrypoint `sdk-cli`) listed as sessions while running |
| Claude desktop | Running ones seen (pid file + hooks). Ended ones gone; desktop titles/archived only in the app's own registry |
| Codex CLI | Only via hooks: sessions predating setup, or with untrusted/disabled hooks, are invisible; threads on the shared daemon share one pid so ending depends on SessionEnd (≤30 min late); approval waits invisible; no ended list |
| Codex in ChatGPT app | Unknown if hooks fire (U); if they do, same shared-pid problem (pid 19747 lives as long as the app) |
| Cursor | IDE sessions refused by caller classification; CLI via hooks only |
| OpenCode | Running via plugin; no waiting state; no ended list |
| Grok | Running via hooks; no waiting state; no ended list |

### What the Lightbulb desktop app sees
- Claude: `claude agents --json` (claude.rs:670-727), so interactive + desktop + background (incl. process-less working/blocked jobs), with `listing_status` (claude.rs:762-776) mapping `state`/`status` incl. `waiting`→`waiting-approval`, and titles from `custom-title`/`ai-title`, lineage via `continued-in`/`parkedJobId`. Note it maps `shell`→idle (Claude maps it to busy).
- Codex: lsof of rollout files each codex process holds open for writing + the shared daemon's loaded-thread list over its control socket (codex.rs:311-345), status/title from the rollout tail (codex.rs:1194). Admits CLI and ChatGPT-app rollouts by source/originator; refuses exec/subagent/legacy.

---

## 6. Recommended discovery source per harness
- **Claude Code (CLI, desktop, background):** `claude agents --json --all` (documented, ≥2.1.145; `--all/state` ≥2.1.169) as the list and state; keep the pid-file scan only for the push socket and as fallback below 2.1.145; titles from transcript `custom-title` > `ai-title`; ended = dropped from the listing → store row with `ended_at`, resume `claude --resume <sessionId>` (background: `claude attach <id>`).
- **Codex:** hooks for presence (add PermissionRequest), plus the shared daemon's app-server `thread/loaded/list` + `thread/status/changed` for busy/waiting when the daemon serves the thread; `thread/list` (or `state_5.sqlite threads`, undocumented) for titles/cwd/recently ended; resume `codex resume <id>`. Verify hooks in the ChatGPT app before relying on them; otherwise fall back to the desktop app's lsof-on-rollouts method.
- **Cursor:** hooks; accept `Cursor.app` as the caller for IDE sessions; resume `cursor-agent --resume <chatId>`.
- **OpenCode:** existing plugin for live state; `opencode session list --format json` for ended; resume `opencode -s <id>`.
- **Grok:** hooks; `~/.grok/sessions/*/*/summary.json` for title/ended (verify `active_sessions.json` schema before using it); resume `grok -r <id>`.
