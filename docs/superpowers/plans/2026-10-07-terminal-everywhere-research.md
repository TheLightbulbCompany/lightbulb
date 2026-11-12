# Research report: terminal everywhere, session chat, browsers (2026-10-07)

Disposable (see CLAUDE.md "Planning docs are disposable"). Written against `main` `dfce908f8`
after fetching origin and refreshing the graphify code graph (post-merge hook, 2026-10-07T01:29Z).
Four sections, as the brief asked: observed (read in code or docs), inferred (reasoning),
locally tested (run on the founder's Mac), production-tested (what earlier sessions verified on
prod; nothing in this research touched prod). The pass/fail/blocked matrix is in
`2026-10-07-terminal-everywhere.md` (plan) and summarised at the end.

## 1. Observed

### 1.1 Shipped since the last session-sharing spec (origin/main, 2026-10-05 → 10-07)
#3750 Allow/Resume confirmed on the Mac (no remote-input freeze), #3754/#3755/#3773 public
session links Phase 1 (Continue), #3764, #3772 who typed + coloured labels, #3780–#3784 Codex
app session verification / approval liveness / hook generation fences (the Codex task),
#3788/#3789 updater pinning and error details (the Codex task), #3785–#3787 Meet voice.

### 1.2 Own notes (verified by reading)
#### OBSERVED in code (read by me)
- LocalSessionPanel.tsx:65-78 decides terminal vs transcript: `hasTerminal = !!session.terminal`; label "Transcript · Ended" / "Transcript · <owner>'s Mac is offline" / "Transcript · <status>" when no terminal; `pressLeft` hint for plain Claude (L80, L234-237).
- LocalSessionPanel.tsx:192-200: under a live terminal `NativeSessionTranscript` still renders with `events=[]` (pending/queued rows only). L258: `liveTerminal ? feedback : local-session-scroll` (transcript list).
- SessionTerminalView.tsx:93 xterm `scrollback: 0`, `disableStdin` unless admitted; frames via `workspace.session.screen` (L112-121); STALE_AFTER_MS 30 s → falls back to transcript (`onLive(false)`), i.e. an AUTOMATIC transcript fallback exists today (L11-12, L116-117).
- SessionTerminalView.tsx:76-80, 126-127: viewer scales the owner's cols×rows to fit (transform scale), never resizes the Mac (comment L23).
- sessionScreen.ts: epoch/seq ordering, HELD 16 early frames, waits for next snapshot (≤10 s).
- session_screen.py:63-108 `watch` (VIEWER# 45 s TTL, WATCH# row, nudge `local.session.watch` only if deviceFeatures has `liveTerminal`); 197-238 `forward` (no storage, per-viewer user channel, 2 MiB/min budget per backend task); 326-407 `type_into` (owner_only authorize, `_still_a_member`, `remoteInput` feature, 4096 B frames, 20 calls/s, 64 KiB/min, TYPED# audit, `fromOwner`, `actor` name).
- local_sessions.py:588-594 `session_terminal(row)`: "held"|"attach"|None; L652 public terminal None when ended/offline.
- desktop local_sessions.rs ~L1000-1017 `NativeSession::terminal`: holder→held, job→attach, else none.
- screen.rs:140-162 `holder_for_session`: held → its holder; Claude background job → attach holder started on demand at the job's tty size (only owner panel may start when size unknown); Codex thread → attach holder (any caller). L124-128 IDLE 60 s, RESTART_GAP 30 s. Relay constants L29-43: CHUNK 11 KiB, FLUSH 200 ms, SNAPSHOT_EVERY 10 s, LEASE 30 s, GRACE 15 s, SNAPSHOT_GAP 1 s, BUDGET 2 MiB/min, BURST 256 KiB.
- holder/protocol.rs: HELLO roles interactive|watch|type|recorder|origin; frames OUTPUT(offset)/INPUT/RESIZE/EXIT/REPLAY(from offset)/GAP/SNAPSHOT/DETACHED/PROMOTED/OWNER_INPUT; MAX_PAYLOAD 8 MiB. holder/run.rs:18 LOG_CAP 1 MiB (in-memory output log), OUTPUT_BACKLOG 8 MiB, EXIT_DRAIN 1 s. No on-disk recording found (grep "record" in holder/*: none).
- holder/manager.rs:70-90: ATTACH_SCRIPT `claude attach <job>`; CODEX_ATTACH_SCRIPT `codex resume <thread> -c check_for_update_on_startup=false --remote unix://`; attach_provider by id shape (8 hex Claude job / UUID Codex thread).
- terminal/terminal.js:5: the desktop's own panel xterm has scrollback 5000 (the web viewer has 0).
- mods/lightbulb-sessions/hooks/register.tsx: band polls mod.sock every 2 s; Allow/Decline/Pause via hotkeys a/d/p; teammate rows redrawn; typed-prompt labels keyed by text; tool.call guard for protected files during teammate turns; `continuedLine`.
- shell_integration.rs: "Share every session live" = `claude`/`codex` shell functions in ~/.zshrc/~/.bashrc (+bash login file) calling `~/.local/bin/lightbulb <tool>`; interactive shells only (`case $- in *i*`); real tool when stdin/stdout not a tty. Not default-on (offered once per Mac, #3716).
- cli.rs: `lightbulb claude|codex|continue`.
- messaging.py:42 RoomKind channel|dm|group-dm; messaging_repo.py:65 MAX_ROOM_MEMBERS 32; repo create_room has `extra: dict|None` (L707); special rooms are id-derived (default_room_id, vendor_room_id) never flagged (L1755-1775).
- routers/container.py: browser routes `/live-view` (L218), `/viewing` (L277), `/mac/input` (L389), `/input` (L437). messaging.py:244,250 `/messaging/sessions/{id}/viewing|input`.
- hosted_mcp.py tools: find_rooms, find_sessions, read_messages, send_message, message_receipt (L239-322). Desktop native_mcp.rs same five (L245-263). Hermes plugin tools: send_room_message, hire_suggestion, lightbulb_local_sessions, lightbulb_voice_profile, lightbulb_desktop (apps/infra/hermes/plugins/lightbulb/__init__.py:364-374).
- cdp_mux.py docstring: ONE AWS CDP connection per browser session; named upstreams; `mac:<device>:<runId>`; cast CLOUD only. mac_cdp.py: ticketed door `ws://<task>:8000/api/v1/internal/mac-cdp/<ticket>` (TICKET 60 s, RECHECK 30 s).
- agent_cast.rs: casts the tab a Mac agent run drives: screencast view tier ≤10 fps ≤200 KB b64 + 480 px thumb every 500 ms; `browser.cast.set {channel, view, thumb}`; TTL 15 s. BrowserFrameView.tsx: `room.browser.frame`, frames sibling channel for Mac view tier, `POST /browser/mac/input` batched 150 ms, MAX_INPUT_EVENTS 32.

#### LOCALLY OBSERVED on this Mac (2026-10-07)
- Claude Code 2.1.292; `claude attach <id|name>` opens a BACKGROUND session only ("← returns to agent view, Ctrl+Z drops back to your shell"); `--remote-control` exists (Anthropic's own same-account Remote Control). `claude agents --json` rows here carry kind/status/name/cwd/pid/sessionId, NO `entrypoint` field (memory from 09-27 said rows had `entrypoint`; not on 2.1.292 output).
- Codex CLI 0.159.3: `agents`, `app-server`, `remote-control` (experimental), `resume --remote <ADDR>`, `--no-daemon`.
- Hosts installed: VS Code + Claude Code extension 2.1.289/290/292, Cursor, Claude.app, ChatGPT.app (Codex desktop), tmux (/opt/homebrew/bin/tmux). Not installed: it2, kitty, wezterm. Terminal.app AppleScript probe timed out (-1712; Terminal not running or no automation consent).
- Running Claude sessions here are `interactive` with controlling ttys (ttys011…ttys030) — i.e. no attach path today; all would show "Transcript · <status>" to a teammate.
- agent-browser CLI is NOT installed on this Mac (it is baked in the Hermes image only).
- Browser automation processes running here: `@playwright/mcp`, `chrome-devtools-mcp@1.8.0` (npm exec) — Claude Code sessions reach Chrome through MCP servers (chrome-devtools-mcp spawns Chrome with a remote-debugging port) and through the Claude-in-Chrome native messaging host (`com.anthropic.claude_code_browser_extension.json` present in Chrome's NativeMessagingHosts) and Codex's (`com.openai.codexextension.json`).

#### OBSERVED in docs/memory (rulings)
- 2026-10-03 owner: NO RECORDING ("sharing a terminal that is running vs not"); 2026-10-04 owner: track every session "including old ones with the same current terminal view"; teammates see it too. Brief 2026-10-07: terminal everywhere incl. ended/offline/earlier history; never a transcript fallback. → the recording question must be re-decided explicitly (listed under owner decisions).
- 2026-10-03: teammates admitted via Ask to join / team control TYPE into the live terminal; everyone else watches read-only; no remote keystrokes from agents; phone keeps session view (now superseded by the brief: terminal everywhere).
- 2026-09-26: consent once at pairing; no per-operation native prompts. 2026-10-02: no Lightbulb approval card for native sends.
- Codex task (local Codex CLI, worktrees under ~/.codex/worktrees/*, branches codex/session-*, codex/updater-*; open PR #3789) owns: codex.rs, transport.rs, lifecycle.rs, local_session_relay.py (#3782), SessionFocus.tsx + localSessionLabels.ts + mobile session screen (#3783), updater routes (#3788/#3789). Detached research worktrees `session-chat-audit`, `session-sharing-audit`.

#### herdr (web, 2026-10-07; v0.9.3, Apache-2.0, YC F26, Rust single binary)
- Model: persistent SERVER owns PTYs + process trees; CLIENTS render the outer UI and stream terminal views; multiple clients may attach to one session; closing a client never stops work; server restart restores layout but NOT processes; experimental `pane_history` persists screen contents across restarts (a sensitive file on disk).
- Hierarchy: Session → Workspaces (per repo) → Tabs → Panes (real PTYs) → Agents (recognised processes). Agent state working/blocked/done/idle/unknown from the FOREGROUND PROCESS + "screen manifests" (pattern-matching the live bottom of the screen), plus optional native integrations; a sidebar attention queue.
- CLI = socket API, JSON out: `herdr agent start <name> --kind codex --pane w1:p2`, `herdr agent prompt <name> "…" --wait --timeout 120000`, `herdr pane split --current --direction right`, `herdr agent explain <name> --verbose`, `herdr machine add <ssh host>`, `herdr --remote <machine>`, `herdr --skill`; prefix keys ctrl+b (v/-/c/z/q), mouse-native.
- No browser/phone client, no multi-user sharing, no file-change isolation; detection brittle to agent UI updates.
- Mapping to Lightbulb: holder == herdr server pane (one process per session instead of one server); `lightbulb claude|codex`, remote start, `lightbulb_desktop` tool == `herdr agent start`; hooks (SessionStart/Stop/PermissionRequest) == state detection (better than screen manifests, worse coverage: no hooks → unknown); All sessions list == sidebar; MISSING vs herdr: every pane is server-owned by construction (we have discovery + attach + shell switch), `pane_history` (we have 1 MiB in-memory LOG_CAP and no persistence), multi-client attach with independent sizes (claude attach size-fights; codex resume does not), agent-driven pane API (`lightbulb_desktop` is agents-only; no CLI for a human to prompt another session; `send_message(session_id)` exists via MCP).

### 1.3 Desktop facts (subagent report, every fact cited; O = observed, I = inferred)
#### 0. Spec deltas worth knowing first

- O: Revision 5 says "any plain Codex session" shows the transcript
  (`docs/superpowers/specs/2026-10-02-managed-session-sharing.md:22-25`). The code no longer does
  this. #3690 (`7ff861eab`, "live terminal for plain Codex sessions") gives a Codex thread on the shared
  daemon an attach terminal (`local_sessions/codex.rs:255-262`, `holder/manager.rs:70-74`). Code wins.
- O: The heartbeat field is `entry["terminal"] = m.native.terminal()` (`local_sessions/transport.rs:2274`).
  It is sent only when the relay advertises `sessionTerminal` (`transport.rs:1421`). The mod socket
  omits `"none"` (`local_sessions/mod_socket.rs:44`).
- O: `NativeSession::terminal()` (`local_sessions.rs:1011-1017`) returns: holder set → `held`;
  no holder but `job` set → `attach`; otherwise → `none`. `holder` comes from `track()`
  (`local_sessions.rs:1259-1270`). `job` comes from the provider capture.

#### 1. Session origins

How the app classifies a session:
- O: Claude sessions come only from `claude agents --json` (`local_sessions/claude.rs:353-356, 380-393`).
  `capture` refuses every row whose `kind` is not `interactive` or `background` (`claude.rs:26-29`).
  Rows it refuses still put their ids in `listed()` (`claude.rs:369-372`), which liveness and the
  resume "is it free" check read.
- O: A Claude session is `held` when `holder_of` finds a holder whose `provider_pid == pid` and
  whose pid is the provider's real parent (`local_sessions.rs:1248-1255`).
- O: A Codex session is `held` when some holder's `.thread` file names its conversation
  (`local_sessions.rs:1263-1265`, `holder/manager.rs:266-281`).
- O: Codex discovery runs `ps -axo pid=,comm=`, keeps every process whose name contains `/codex`
  (`codex.rs:330-352`), and reads the rollouts that process has open for write via `lsof -Fan`
  (`codex.rs:231-253, 271-290`). A rollout is accepted only if its source is `cli`, or `vscode` with
  originator `Codex Desktop` (or `codex-tui` at an accepted version) (`codex.rs:12-17, 42-50, 158-180`).
- O: Lightbulb's own attach clients are removed before classification (`local_sessions.rs:1261`).

| # | Origin | How discovered | `terminal` | Why |
|---|---|---|---|---|
| a | Plain interactive `claude` in Terminal / iTerm / tmux | agents row `kind:"interactive"` | **none** | O: no holder is its parent (`local_sessions.rs:1251-1254`). `job_id` needs `kind=="background"` (`claude.rs:345-351`). Test "plain" → None (`local_sessions.rs:1991-1999`). UI says "Press ← …" (`apps/frontend/src/components/messaging/LocalSessionPanel.tsx:234`). |
| b | `claude --bg` background job | agents row `kind:"background"` + 8-hex `id` | **attach** | O: `job = job_id(row)` (`claude.rs:31, 345-351`). The relay starts `claude attach <job>` in an attach holder (`holder/manager.rs:69, 86-90`; `local_sessions/screen.rs:144-156`). |
| c | Started by Lightbulb (New session, remote start, pick-up, Resume, `lightbulb claude`) | agents row, `interactive` | **held** | O: the provider is the holder's direct child. With a start link, `/bin/sh -c '… exec "$@"'` keeps the pid (`session_start.rs:356-371`). The CLI path: `cli_start` → holder (`session_start.rs:483`). Codex is held through the thread binding. |
| d | A session moved with ← (fork) | `background` row | **attach** (fork). The origin is **none** until it ends | O: `forked_from` comes from `ps -ww -o command=` `--fork-session --resume …` (`claude.rs:153-162, 206-214`) or from `parkedJobId` in `~/.claude/sessions/<pid>.json` (`claude.rs:218-236`). The listing shows only the fork; the origin ends when the fork is tracked (spec :19-25; `transport.rs:657`). |
| e | `claude -p` / sdk-cli | I: if `agents --json` lists it at all, its kind is not `interactive`/`background` | **not tracked** (I) | O: any other kind → `Err("not a Claude Code session")` (`claude.rs:27-29`). Test: `kind:"cron"` is refused (`claude.rs:1579`). `lightbulb claude -p` runs the real tool (`cli.rs:91-107, 552-559`). |
| f | Claude Code in VS Code extension / JetBrains | I: the extension's own process probably appears with a non-interactive kind, or as interactive with no tty | none, or not tracked (I) | O: there is NO entrypoint/IDE handling anywhere. A grep for `entrypoint\|vscode\|jetbrains` in the Claude code finds only Codex constants, and `CLAUDE_CODE_ENTRYPOINT` appears only as a marker the holder strips (`holder/run.rs:149-153`). I: `claude` typed into an IDE's integrated terminal is an interactive shell, so it is case (a). With the shell switch it becomes case (c). |
| g | Claude Desktop app, Code tab | I: SDK-driven, not `interactive` | not tracked or none (I) | O: no code names the Claude Desktop app. The only `ClaudeCode.app` handling is the binary-identity hard link (`local_sessions.rs:1088-1095`). |
| h | Plain `codex` on the shared daemon | the daemon pid under `~/.codex/packages/app-server-daemon/releases/<v>-<arch>-apple-darwin/bin/codex` holds the rollout | **attach** | O: `daemon_release` matches (`codex.rs:70-83`). `attach_thread` sets `job = thread` (`codex.rs:259-262, 304`). The attach argv is `codex resume <thread> -c check_for_update_on_startup=false --remote unix://` (`holder/manager.rs:74`). |
| i | `codex --no-daemon` (plain) | the TUI process writes its own rollout | **none** | O: no `daemon_release` → `job` None (`codex.rs:259-262`). No holder thread binding exists. Through `lightbulb codex`, the CLI always adds `--no-daemon` (`session_start.rs:458-465`) and the session is **held**. |
| j | Codex desktop app (ChatGPT.app / Codex.app) threads | the app's own `codex app-server` pid. One pid holds every open thread (`codex.rs:264-270`). Originator `Codex Desktop` | **none** | O: the binary is under `/Applications/ChatGPT.app/Contents/Resources` (`codex.rs:27, 52-64`), not the daemon, so `job` is None. The VS Code Codex extension (originator `vscode-extension`) is refused outright (`codex.rs:14-17`; test `codex.rs:1898-1900`). |
| k | Codex app sessions after a bundle update (#3780, `a8c2e4aa6`) | as (j) | **none** | O: #3780 only changes version verification. `desktop_upgrade` accepts a paginated `Codex Desktop` rollout whose header `cli_version` is older than the running bundle (`codex.rs:188-195`). It affects `restriction`, not `terminal`. |

#### 2. The screen relay (`local_sessions/screen.rs`)

- O: **One relay per session, not per viewer.** `RELAYS: BTreeMap<sid, Arc<Ctl>>` (`screen.rs:53`).
  Each relay is a single holder `watch` client (`screen.rs:396-426`). The backend fans frames out to
  each viewer's user channel (spec §5.5).
- O: **Start.** A `local.session.watch` nudge (`screen.rs:347-381`). A nudge sets `keep = now + GRACE`
  (15 s) and renews the lease (`screen.rs:79-84, 37`). The backstop is the heartbeat-ack
  `watching` list (`offer`/`settle`, `screen.rs:308-343`).
- O: **Stop.** The relay stops when it leaves `watching` and its GRACE has passed (`screen.rs:320-327`), when
  no ack renews it for `LEASE` 30 s (`screen.rs:33, 88-90`), on a holder EXIT (`screen.rs:545-550`), or
  when the holder closes (`screen.rs:552`).
- O: **Cadence.** `CHUNK` 11 KiB (`:29`), `FLUSH` 200 ms (`:30`), `SNAPSHOT_EVERY` 10 s (`:31`),
  `SNAPSHOT_GAP` ≥1 s between snapshot requests (`:40`), `TICK` 50 ms (`:34`).
  `Budget`: a token bucket with `BURST` 256 KiB, refilled at 2 MiB/min (`:42-43, 428-456`). Output
  that does not fit is dropped and marks the stream `stale`. Snapshots may overdraw (`:446-455, 470-492`).
- O: **A fresh snapshot is sent** when the viewer set grows (ack `since` increases, `plan`
  `:296-305`), on a nudge for a running relay (`:370-374`), after a resize (`:537-544`), after a lost
  frame (`:488-492`), and every 10 s (`:519-524`). The snapshot is requested with an empty SNAPSHOT
  frame to the holder (`:522`).
- O: **Snapshot content** = `alternate-screen flag + vt100 screen.state_formatted()`
  (`holder/model.rs:89-97`). That is the visible screen and its modes. **No scrollback.** The vt100
  parser keeps 5000 scrollback rows (`holder/model.rs:57`), but `snapshot()` never sends them.
- O: **REPLAY (kind 6) is never used by the relay or the panel.** The only `replayFrom` user is the
  CLI's HELLO (`cli.rs:478`, `replayFrom: 0`). No code sends a REPLAY frame. The holder only answers one
  (`holder/run.rs:395-406`).
- O: **Viewer side.** The web viewer runs xterm with `scrollback: 0`, `disableStdin` unless the viewer
  may type (`apps/frontend/src/components/messaging/SessionTerminalView.tsx:93`). It calls
  `term.reset()` on every snapshot, resizes to the frame's cols/rows, and CSS-scales to fit (`:76-87`).
  It never resizes the Mac (`:23`). So viewers have no history to scroll, and one viewer cannot change
  another's view.
- O: **Owner resize → viewers.** The holder resizes the pty and model only on a RESIZE from the
  interactive client (`holder/run.rs:383-389`, and on interactive HELLO with cols/rows `:332-338`).
  It then sends RESIZE to watch clients (`tell`, `:469-475`). The relay emits pending output at the old
  size, switches size, and asks for a snapshot (`screen.rs:537-544`).
- O: **Who is the interactive client.** The owner's panel HELLOs `{"role":"interactive","join":true}`
  (`holder/panel.rs:150`). With no terminal attached, it IS the interactive client, and every window
  resize sends RESIZE (`terminal/terminal.js:38-44, 86`). So the owner resizing the app's panel
  resizes the provider's pty for every viewer. When joined behind a `lightbulb claude` terminal,
  the panel scales and never resizes (`terminal.js:21-37, 75-79`).
- O: **Attach-holder sizing.** A Claude attach holder starts at `tty_size(pid)` of the job
  (`screen.rs:150, 185-212`). `tty_size` runs `ps -o tty= -p <pid>`, opens `/dev/ttyNNN` with
  `O_NOCTTY|O_NONBLOCK`, and reads `TIOCGWINSZ`. If the size is unknown, only the owner's panel may
  start one (`start_size`, `:188-190`). Reason given: "the job draws at the latest attach size" (`:146-147`).
  A Codex attach holder starts with no size (default 120x40, `holder/run.rs:19-20, 163`) because
  "each client draws at its own size" (`screen.rs:146-150`; `holder/manager.rs:70-73`).
- I: **Size fight.** A `claude attach` client redraws the job at its own size. When the owner's
  panel becomes the interactive client of an attach holder and resizes it, the job redraws at the
  panel's size, and any other attach client (for example the owner's own `claude attach` in iTerm)
  sees a mis-sized screen. Lightbulb avoids this only by starting at the job's current size.
- O: **Attach lifecycle.** An attach holder that is unused (not shown, not watched) for `IDLE` 60 s is
  stopped, and also when its job is no longer listed (`screen.rs:125-128, 224-278`). Restarts are
  limited to one per `RESTART_GAP` 30 s per job (`:128, 172-183`). A stop kills only the client
  (SIGTERM to the group, SIGKILL after 2 s), never `claude stop` (`holder/manager.rs:103-151`).
- O: The relay stores and queues nothing. The outbound queue holds 8 items, and a full queue counts as a lost frame
  (`screen.rs:94-107`). Frame shape: `local.session.screen {sessionId, generation, epoch, seq, cols,
  rows, kind, data(base64)}` (`screen.rs:561-575`).

#### 3. The holder (`holder/`)

- O: **Output log.** `LOG_CAP` 1 MiB `VecDeque` ring buffer with absolute offsets `total` (`run.rs:18, 264-265`).
  `OUTPUT_BACKLOG` 8 MiB: past it the pty reader sleeps (`run.rs:24, 212-228`).
  `EXIT_DRAIN` 1 s: output is read for this long after the provider exits (`run.rs:26, 281-287`).
  `CLIENT_QUEUE` 1024 frames per client before it is disconnected (`run.rs:22, 479-508`).
- O: **The exact append loop** (the place to hook a recording) is `Event::Output(bytes)` at
  `holder/run.rs:294-306`: `model.feed` → `encode(OUTPUT, with_offset(total, &bytes))` →
  `total += len` → `log.extend(&bytes)` (`:302`) → trim to LOG_CAP → fan out to clients.
  Size changes are at `:332-338` (interactive HELLO) and `:383-389` (RESIZE). Exit is at `:429-451`.
- O: **Model.** `vt100::Parser::new(rows, cols, 5000)`. It answers DA/XTVERSION/OSC10/11/CPR probes itself
  (`model.rs:7-16, 61-78`) and drops probe replies from clients (`run.rs:377-378`, `model.rs:102-114`).
- O: **Roles** (`protocol.rs:5-13`, `run.rs:310-376`):
  - `interactive`: owns size and keyboard. It gets a SNAPSHOT, or OUTPUT from `replayFrom`.
  - `interactive`+`join`: watches and types at the other client's size, and is promoted when that client leaves (`:316-330, 415-427`).
  - `watch`: a snapshot, then output and RESIZE.
  - `type`: input only.
  - `origin`: facts only (`lastInput`).
  - **`recorder`**: O: it has no branch of its own. It falls through to `clients.insert` (`run.rs:375`),
    so it is an output-only client with no snapshot. Its only user is `manager::hello`, which sends
    `{"role":"recorder"}`, reads HELLO and drops the connection (`holder/manager.rs:243-262`).
    **There is no on-disk recording anywhere.** A grep for `recorder|asciicast` finds nothing else.
- I: **Recording with a small change.** At `run.rs:302`, also append
  `[elapsed_secs, "o", utf8_lossy(bytes)]` lines (asciicast v2) to a 0600 file beside the socket or in
  app data. At `:337`/`:388`, append `"r"` events. At start, write the header (`cols`, `rows`, start time).
  Open questions:
  - A cap or rotation is needed, because output is unbounded.
  - The file holds everything the terminal showed, secrets included. Its retention, upload and
    redaction need an owner decision.
  - The holder dies with the provider (below), so the file outlives it.
- O: **Panel attach.** The panel is a child webview served from the loopback (`holder/panel.rs:40-130`).
  Its HELLO is `interactive`+`join`+size, with no `replayFrom` (`panel.rs:148-158`). So it gets a
  **snapshot only**: its xterm `scrollback: 5000` (`terminal/terminal.js:5`) fills only with output that
  arrives while it is open. The CLI asks for `replayFrom: 0`, which gives up to the last 1 MiB (`cli.rs:476-478`).
- O: **Survival.** The holder is spawned with `setsid` and null stdio. It is reaped by a thread, or by
  launchd after an app restart (`holder/manager.rs:186-203`). It ends when its provider exits and EOF is
  drained (`run.rs:432-451`): it sends EXIT and removes `.sock` and `.thread`. The panel End sends SIGTERM
  to the group (`manager.rs:62-64`, `panel.rs:172-181`). For attach holders, `stop_attach` does it.

#### 4. Remote input (`local_sessions/remote_input.rs`)

- O: Input is admitted only for a live relay of that sid at that generation, at the relay's current
  `epoch` (`:165-177`, `screen.rs:357-361`). Base64 ≤ `MAX_INPUT` 4096 raw bytes (`:28, 166, 173`).
- O: **What is stripped.** Probe replies are always stripped. When `fromOwner != true`, these "band keys" are
  also stripped: legacy ctrl+x `0x18`, CSI-u ctrl+x, and every mouse report (SGR, urxvt, X10)
  (`:144-145` doc, `:236-249` `strip_remote`, `:330-350` `strip_pass`).
  `Carry` holds an unfinished escape across frames, so `ESC[120;5` + `u` cannot add up to ctrl+x
  (`:247-303`). Known cost: Shift/Ctrl+F3 is dropped (`:329`).
- O: **Writing.** One `type` client per relay on its own writer thread. A queue of 64 calls; overflow
  is dropped (`:30, 145-161, 180-216`). Owner frames go out as `OWNER_INPUT` when the holder HELLO
  says `origin:true` (`:204`). The holder sets `lastInput = "teammate"` only for a `type` client's
  plain INPUT (`holder/run.rs:377-382, 458-460`).
- O: **typedBy.** `typed()` records the frame's `actor` (userId, name) only when the frame contains
  CR/LF (`:75-85`). `typed_by` names that teammate only when the holder's `lastInput == "teammate"`
  and the Enter came within `SUBMIT_WINDOW` 3 s (`:101-107`). `teammate_holds_decision` uses
  `TEAMMATE_QUIET` 2 s (`:39, 120-122`).
- O: **Buffering.** No per-typist draft or line buffer exists. The web viewer batches `onData` per
  animation frame and sends it in ≤4096-byte chunks, one call at a time (`SessionTerminalView.tsx:62-75,
  103-107`). The Mac writes each frame with a single `write_all` (`run.rs:381`). Keystrokes are raw,
  and every typist and the owner edit the same provider input line.
- I: **An atomic "whole line + Enter" send** would need:
  1. A compose box on the viewer that sends `text` (wrapped in bracketed paste `ESC[200~…ESC[201~`,
     if the provider enabled mode 2004) plus `\r` as ONE input call ≤4096 bytes. The holder already writes
     one frame contiguously.
  2. A rule for the owner's half-typed draft. The line lands in the same Claude/Codex prompt buffer
     and is appended to whatever is there. Options: refuse while the prompt is not empty (not
     observable without screen parsing), or route through the structured send path instead
     (`local.session` commands), which does exist.
  3. The 2 s quiet / band rules stay unchanged.

#### 5. Ended / offline / resume

- O: **Ending.** `local.session.ended {sessionId, generation, reason, endedAt}` is built in
  `transport.rs:756-767`. The one end path is a SessionEnd hook, a pid that is gone, or a fork that
  replaced it (`transport.rs:657-686`; `lifecycle.rs:361`; reason `exited` / `process-exited`
  `transport.rs:830`). The holder's EXIT frame does NOT produce it. EXIT only stops the relay
  (`screen.rs:545-550`) and shows "This session has ended." in the panel (`terminal.js:82`).
- O: **Resume.** Claude: `claude --resume <id> --permission-mode plan|manual`. Codex: `codex --no-daemon
  --sandbox … --ask-for-approval on-request resume <thread>` (`resume.rs:88-113`). It starts through
  `start_held` (the holder's direct child, no window) (`resume.rs:193-228`, `session_start.rs:346-354`).
  It refuses if a holder argv already names the conversation (`resume.rs:266`) or if any local
  process lists it (`resume.rs:205-208, 231-260`).
- O/I: **Same session id.** Session id = uuid5(owner, device, provider, profile, conversation)
  (spec `:235`, assigned on register ack `transport.rs:1458`). The generation = pid + birth.
  I: `claude --resume <id>` without `--fork-session` keeps the conversation id, so the resumed
  session keeps the same sessionId with a new generation. The spec's "first new-generation
  registration consumes the waiting resume" (spec §5.7) depends on this.
- O: **Old output before the resumed process draws.** Nothing can replay it. The old holder
  removed its socket at exit (`run.rs:448-449`), its 1 MiB log was in memory only (`run.rs:264`), and
  nothing is written to disk (§3). The relay keeps nothing (`screen.rs:1-8`). Confirmed: without a
  recording, an ended or offline session has no terminal image. Only the transcript remains.

#### 6. "Share every session live"

- O: **What it does.** It writes a marked block into `~/.zshrc`, `~/.bashrc` and the bash login file in use
  (`shell_integration.rs:7-20, 72-90`). The block defines `claude` and `codex` functions for
  **interactive shells only** (`case $- in *i*`). When stdin AND stdout are ttys, they call
  `~/.local/bin/lightbulb <tool> "$@"`, else `command <tool>` (`shell_integration.rs:32-43`).
- O: **The CLI.**
  - Non-interactive forms (`-p`/`--print`, informational flags, Codex non-TUI subcommands) run the real tool (`cli.rs:91-107, 552-559`).
  - A non-tty runs the real tool (`cli.rs:563-566`).
  - "App not running" → message and exit 1 (`cli.rs:592-595`).
  - Other refusals → the real tool with a "no live terminal" note (`cli.rs:596, 336-338`).
  - Codex gets `--no-daemon` (`session_start.rs:458-465`).
- O: **When it is offered.** The pairing dialog has a "live" row, **default off** (`useState(false)`,
  `apps/frontend/src/components/desktop/DesktopPermissionsGate.tsx:53, 115-118, 130`).
  `LiveSharingOffer` shows once per Mac, as a toast with "Turn on" / "Not now", when sharing is on and
  the switch is off (`apps/frontend/src/components/desktop/ExplicitSharingNotice.tsx:29-69`,
  mounted in `apps/frontend/src/app/layout.tsx:111`). Settings row: `LocalSessions.tsx:75-94`.
- I: **What it cannot cover:**
  - Non-interactive shells and scripts.
  - Shells started before the switch was turned on (new tab needed; the toast says so).
  - fish and other shells (only zsh/bash files).
  - IDE/app-spawned binaries that exec `claude`/`codex` directly (VS Code/JetBrains extensions, Claude Desktop, ChatGPT.app, Codex.app).
  - `claude --bg`: `cli.rs` has no `--bg` handling (grep), so `lightbulb claude --bg` would put the job launcher in a holder. The job itself is covered by attach.
  - `codex resume`/`claude --resume` typed by hand: these are covered, since args pass through.
  - Absolute-path invocations (`~/.local/bin/claude`) and aliases defined after the block.
  - tmux panes are covered only if their shell reads the rc files (it normally does).

#### 7. Gaps vs "every session shows its real terminal" (all I)

| Origin today | Shows | Cheapest real-terminal mechanism |
|---|---|---|
| Plain `claude` in a terminal (a) | transcript | (i) the switch default-on at pairing (one boolean, `DesktopPermissionsGate.tsx:53`) for future sessions. (ii) ← adoption for live ones (an owner action). (iv) a host-terminal screen reader for the rest. |
| Plain `codex --no-daemon` (i) | transcript | (i) the shell switch. Otherwise (iv). It is not on the daemon, so (iii) `codex resume --remote` cannot join it. |
| `claude -p` / sdk / Claude Desktop Code tab (e, g) | not tracked | No TUI exists to show. (v) window capture only for Claude Desktop. |
| VS Code / JetBrains Claude extension (f) | none or not tracked | (v) ScreenCaptureKit window capture (pixels; the IDE window, not just the terminal). |
| Codex desktop app threads (j, k) | transcript | (iii) does not apply: the app's own app-server is not the shared daemon (`codex.rs:255-258`). Option: (v) window capture. I: (iii) might work if `codex resume <thread> --remote unix://<app-server socket>` were supported. Not verified. |
| VS Code Codex extension | not tracked | (v). |
| Any session, earlier history / reconnect | screen only | Send scrollback. The holder already has 5000 vt100 rows (`model.rs:57`) and the 1 MiB log (`run.rs:264`). A viewer-side REPLAY from `oldest` (`run.rs:395-406`) or a scrollback-inclusive snapshot needs a relay/backend frame kind and viewer `scrollback>0` (`SessionTerminalView.tsx:93`). Per-viewer scroll stays local because the viewer never resizes. |
| Ended / offline Mac | nothing | (vi) recording at `run.rs:302`, uploaded or served on demand. Offline needs an upload. |
| Owner panel resize | changes all viewers | Viewers already scale. The fight is between the owner's panel and the owner's terminal only (§2). |

**(iv) Host-terminal screen reader: what the app already knows.**
- O: the provider pid (`NativeSession.process`).
- O: its parent (`native_tools::parent`, `local_sessions/native_tools.rs:71`; used `local_sessions.rs:1252`).
- O: its controlling tty name via `ps -o tty=` plus `TIOCGWINSZ` (`screen.rs:192-212`).
- O: open files via `lsof -Fn` (`local_sessions.rs:1075-1086`) and `-Fan` (`codex.rs:231-253`).
- O: unix sockets via `lsof -U` (`codex.rs:98`).
- O: the full command line via `ps -ww -o command=` (`claude.rs:155`).
- I: From the tty (`/dev/ttys012`) the host can be found:
  - tmux: `tmux list-panes -a -F '#{pane_tty} #{pane_id}'`, then `capture-pane -e -p -S -5000` (with scrollback).
  - iTerm2: the Python API or AppleScript `tty of session` → `contents`.
  - Terminal.app: AppleScript `tty of tab` → `history`/`contents` (plain text, no colour).
  - kitty: `kitty @ ls` (`foreground_processes`) → `get-text --ansi --extent all`.
  - WezTerm: `wezterm cli list --format json` (`tty_name`) → `get-text --escapes`.
  - Walking parents from the provider pid to the terminal app pid (an `lsof` of `/dev/ptmx` on the
    terminal process, or parent-walk) identifies which app owns it.
- I: Costs of (iv):
  - These are polled screen dumps, not a byte stream. A ~1 s poll fed into the existing snapshot frame
    kind fits `kind:"snapshot"` exactly.
  - Typing would need `send-keys` / AppleScript `write text`, and Terminal.app cannot type into a background tab.
  - The Automation (AppleEvents) consent prompt is per target app.
  - None exists in the code today. The only `osascript` use is Safari activation (`lib.rs:1799`).
- I: **(v) ScreenCaptureKit.**
  - The app already has Screen Recording permission plumbing: `CGPreflight/RequestScreenCaptureAccess`
    in `screen.rs:27-98` (top-level `src/screen.rs`), today used for a whole-display `screencapture(1)`
    snapshot (`screen.rs:213-218`).
  - A window would be matched by owning pid (the IDE/app pid from the parent walk).
  - Pixels need a new frame type, a video/JPEG path and budget. The 2 MiB/min text budget (`screen.rs:42`) is too small.
  - It cannot be typed into safely.

**(i)/(ii)/(iii) cost summary.**
- (i): a frontend default flip plus copy. It covers only new interactive zsh/bash launches.
- (ii): exists. It needs the owner at the terminal.
- (iii): exists for daemon Codex threads. It does not reach `--no-daemon` or app threads.

### 1.4 Backend, web and phone facts (subagent report, cited; OBS/INF marked)
#### 0. Spec conflict (read first)
docs/superpowers/specs/2026-10-02-managed-session-sharing.md decisions: #4 "Nothing is recorded or stored. A session that is not running shows the existing session view." and #8 "The phone keeps the session view (the structured record)... It has no terminal." (L82-90). The new goal reverses both. (OBS)

#### 1. Public session contract
`LocalSession` = packages/shared/src/messaging/types.ts:366-441. Python source of truth: STATUSES apps/backend/core/services/local_sessions.py:27; `_public` allowlist projection L576-581; `_session_public` L638-~715.
Fields (OBS, types.ts:366-441): workspace?, sessionId, deviceId, humanUserId, provider(claude|codex), generation, status(idle|busy|waiting-approval|unknown|offline|ended), providerVersion, startId? (owner only; popped for others, local_sessions.py:646), controlUnavailableReason, activeTurnId, capabilities, capabilitiesVerified, latestEventSeq, kind?(interactive|background), terminal?("held"|"attach"), forkedFrom?, title?, displayTitle?, cwdName?, lastEventAt?, endedAt?, lastAssistantMessage?, shared?, teamControl?, allowedSenders?, joinRequest?, joinRequestId?, pendingJoins?, pendingJoinIds?, inputPaused?, pickupRequest?, pendingPickups?, pendingPickupIds?, pendingApproval?, model?, permissionMode?, usage?, shareUsage?, pickup?, pickedUpFrom?, resumeAllowed?, resume?, resumable?.
Key derivations (OBS, local_sessions.py `_session_public`):
- `latestEventSeq` = row.eventSeq (L642). `shared` = not row.unshared (L645).
- status: `endedAt != None` -> "ended" (L648-649); else `onlineUntil <= now` -> "offline" (L650-651); else stored status (lease row field `online_until`, L130; `ended_at` L255).
- `terminal` = None if status in (ended, offline) else `session_terminal(row)` (L652): row.terminal in held/attach, else "held" iff row.holderId and terminal unreported (L586-591). => today offline/ended can NEVER carry `terminal`.
- `teamControl` = `team_control_on` = row.teamControl and not ended (L760-762); `allowedSenders` likewise cleared on end (L765-766).
- `inputPaused` (L690-697): only an older presence Mac (FEATURE without BAND_OWNER_ONLY) with pending joins/pickups and terminal != None.
- `pickup` only if row.pickupAllowed or viewer is owner (L712-713); `_pickup_public` L716.
- `forkedFrom` is passed through raw from the row; `_without_moved` L605-636 hides a stopped origin once a fork continues it, and hides `ended_empty` rows (Claude, ended, eventSeq 0).
- `capabilities` zeroed if not `capabilitiesVerified` (L653-654). `resumable` = ended and not empty and kind != background and `session_resume.submissions_enabled` (L663-665).
- `pendingApproval` dropped unless status == waiting-approval (L709).

#### 2. Routes and auth for the live terminal
- `POST /messaging/sessions/{id}/viewing` (204) and `POST .../input` (204): apps/backend/routers/messaging.py:244-254. Both `Depends(get_human_session)` (OBS).
- `get_human_session` apps/backend/core/auth.py:255-276: `get_current_user` then ONLY a first-party Clerk SESSION token. 403 `HUMAN_SESSION_REQUIRED` for `typ: at+jwt`, a `client_id` claim, or no `sid` (JWT-template tokens). So MCP/OAuth tokens and `lightbulb-appsync` template tokens are refused. Web, desktop webview, mobile use the default session token.
- `session_screen.watch` (session_screen.py:63-108): `sessions.authorize_session(auth, None, id)` = roomless read path: any workspace member may READ; `AgentInvocation` raises LOCAL_SESSION_FORBIDDEN (local_sessions.py:914-916); `unshared` reads as 404 to everyone but owner (L922-924). Then `_still_a_member` (L52-60: removed member's user stream inactive -> FORBIDDEN; owner exempt). Then 409-class LOCAL_SESSION_OFFLINE if ended/unshared/lease lapsed (L67-69); LOCAL_SESSION_UNSUPPORTED if `session_terminal(row) is None` ("Its transcript is shown instead", L70-73). Writes `VIEWER#<user>` lease (TTL `VIEWER_TTL_S`=45, L36) and `WATCH#<device>#<sid>` row on the owner's user partition; on a fresh lease and Mac advertising `liveTerminal`, `_nudge` sends `local.session.watch` over the relay (L111-123). Mac reads `watching` from the heartbeat ack (`ack_with_watching` L126-147, ACK_CAP 24).
- `session_screen.type_into` (L326-~395): `authorize_session(..., owner_only=True)` -> `may_control(row,user)` = owner OR `team_control_on` OR in `allowed_senders` (local_sessions.py:769-772; roomless owner_only branch L927-936). Agents refused (no roomless path; room id not accepted). Also `_still_a_member`, offline/unsupported checks, requires device feature `remoteInput`; rate limits INPUT_MAX_RAW 4096, 20 calls/s, burst 40, 64 KiB/min (L244-250); audit row `TYPED#<user>#<burst>` with byte COUNT only (30-day TTL); envelope `local.session.input` carries `fromOwner` flag + `actor` name for non-owners.
- Refused callers (OBS): agents (any), room-scoped callers, MCP/OAuth tokens, removed members, non-admitted humans on /input (read-only watch still ok), everyone when `unshared` (except owner).
- Phone can call both: shared client has `watchLocalSession` / `sendLocalSessionInput` (packages/shared/src/messaging/client.ts:99,101,180-181; `/viewing` and `/input {data, epoch}`). Mobile uses Clerk default token (INF: same client as web).

#### 3. Live events
- Mac -> relay frame `local.session.screen` (`Screen` model session_screen.py:157-171: sessionId, generation, epoch, seq, cols<=1000, rows<=500, kind snapshot|output, data base64 <= 11 KiB raw `MAX_RAW_BYTES` L150). `forward` (L197-241): validates the frame is from that Mac's connection/generation, reads `VIEWER#` rows with unexpired lease, spends budget (`BUDGET_BYTES_PER_MINUTE` 2 MiB/session, L151), builds `make_live_event("workspace.session.screen", ...)` and `publisher.publish_user(owner, user, event)` per viewer (AppSync `/ws/<ws>/users/<opaqueKey>`). NOTHING STORED; returns early if no viewers. Never raises.
- Desktop: SNAPSHOT_EVERY 10 s, SNAPSHOT_GAP 1 s (apps/desktop/src-tauri/src/local_sessions/screen.rs:31,40). Streams only while someone watches.
- Type: `SessionScreen` packages/shared/src/live/types.ts:226-229 `{sessionId, generation, epoch, seq, cols, rows, kind: "snapshot"|"output", data}`; validated in the event guard L410-~420; exported index.ts:54. Ordering helper (web-only): apps/frontend/src/components/messaging/sessionScreen.ts (`receiveScreen`, holds up to 16 early frames, waits for next snapshot). INF: this must move to packages/shared for the phone.
- Phone: apps/mobile/src/providers/live-events.tsx (OBS) uses shared `useLiveEvents` with `userChannel` `/ws/<workspaceId>/users/<userChannelKey>` (L~137), `dispatchEvent` fans EVERY LiveEvent to `eventHandlers` (L120-130); `onEvent(handler)` exposed (L~170). So `workspace.session.screen` already arrives on the phone and can be handled; nothing registers for it (grep: no "session.screen" in apps/mobile). Caveat: `enabled: foreground && httpReady` (L~149): socket is off while app backgrounded, so a backgrounded phone gets no frames and must wait for the next snapshot (<=10 s) on return.
- Web viewer: apps/frontend/src/components/messaging/SessionTerminalView.tsx (129 lines), `@xterm/xterm ^5.5.0` (apps/frontend/package.json:31), watch every 15 s (`WATCH_EVERY_MS`), `STALE_AFTER_MS` 30 s falls back to transcript.

#### 4. Transcript pipeline: keep / retire
Storage: `EVENT#<20-digit seq>` rows in the messaging table partition `LOCALSESSION#<scope>#<id>` (apps/backend/core/repositories/local_sessions_repo.py:4,54); written by local_session_relay.py (L857, L885 `sk=EVENT#...`, L918 `eventSeq=end`). Read by `local_sessions.list_events` (L1258-1268) via 3 routes in routers/messaging.py: agent `GET /messaging/local-session-agent/sessions/{id}/events` (L168-176), human `GET /messaging/sessions/{id}/events` (L418-425), public link `POST /public/session-links/events` (L387-391 -> session_links.list_events L258).

| Piece | Consumed by non-human-transcript things? | Verdict |
|---|---|---|
| EVENT# rows + `events` relay frames (local_session_relay.py) | YES: agents' `lightbulb_local_sessions read` (plugin apps/infra/hermes/plugins/lightbulb/__init__.py:~250-262 calls `/local-session-agent/.../events?afterSeq=&limit=`); public link page (`app/s/page.tsx:93` posts `/public/session-links/events`, renders `NativeSessionTranscript` L185); `eventSeq` cursor drives `lastAssistantMessage`, push copy (relay L317-356), `ended_empty`, `_without_moved`, `latestEventSeq` | KEEP (agents + links + cursor) |
| `GET /messaging/sessions/{id}/events` human route | web/phone only for transcript, plus status derivations below | KEEP while SessionOverview/pending prompt use it; retire only if all human consumers go |
| `/local-session-agent/.../events` + `lightbulb_local_sessions read` | agents; SKILL.md:47 tells agents to read `afterSeq: max(0, latestEventSeq-40)` before editing in a repo | KEEP (hard requirement) |
| `useLocalSessionHistory` (packages/shared/.../useLocalSessionHistory.ts) | web LocalSessionPanel, mobile session screen, SessionDetails (L~199 `hasChanges`), SessionOverview | RETIRE from the main view; KEEP if Details/Overview stay |
| `useHistorySource` (same file L24-40; origin fallback when `latestEventSeq === 0` and `forkedFrom`) | only picks which session's events to read | RETIRE with the transcript; a terminal has no such need (INF: recording of the fork is empty until its next turn -> the same "moved to background" gap reappears for a recording unless the origin's recording is chained) |
| `NativeSessionTranscript.tsx` (web) / `TranscriptRow.tsx` + FlatList (mobile) / `localSessionTranscript.ts` (`buildRows`, `groupRows`, `toolHeader`, `diffLines`...) | public link page `/s` uses it; `pendingApproval()` (localSessionTranscript.ts:757) and `mainEvents` used by status/approval logic | RETIRE for people, EXCEPT `/s` public page (Phase 1 "Continue" is a transcript-only page; spec 2026-10-05 §3.2/§3.7) and `pendingApproval`/`mainEvents` |
| `SubagentPanel`, `sessionSubagents`, `subagentStrip` | only the transcript view (INF) | RETIRE |
| `sessionPlan` / `sessionChanges` (localSessionOverview.ts:33,79) | web SessionOverview.tsx:21-22, SessionDetails.tsx:199, mobile SessionOverview.tsx:26-27 (Details sheet + "N files changed"/plan status). Pure functions of events | KEEP (status line/Details; no terminal equivalent) |
| `pendingApproval(events)` / "waiting-approval" banner | panel L99,243-248 and mobile L353-354 | KEEP (a terminal cannot be answered by phone tap; session.pendingApproval comes from the row, not events) |
| `SessionApprovalCard` (mobile) + web approval card; `session.pendingApproval` | row field set by Mac; command route `approve` (`_require_pending_approval`, local_sessions.py:1280) | KEEP (rows + commands, not events) |
| `usePendingPrompt` / `pendingPrompt` (useSessionCommand.ts:58-66) | shows the sent-but-not-echoed prompt row in the transcript | RETIRE under a live terminal (already hidden: LocalSessionPanel passes `events=[]` when liveTerminal, L192); KEEP command attempt/receipt machinery |
| `useSessionCommand`, `useSessionOutbox`, receipts (`receiptChip`) | follow-up send, offline queue, cancel (mobile L338-351, web L86-97), agents' commands route | KEEP (composer remains for non-terminal states and for agents) |
| `useSessionResume`/`SessionResume` | ended sessions | KEEP |
| `session_links.list_events` + redaction (`session_redact.py`) | public copy/Continue | KEEP |
| `lastAssistantMessage`, `title`, status line "Transcript" label | row fields from relay `_PUBLIC_FIELDS` (relay L272) | KEEP |
Search: message_search.py has no local-session indexing (grep, OBS: only botocore session import) so search does not depend on EVENT#.

#### 5. Storage rail for a recording
Pick-up (apps/backend/core/services/session_pickup.py, OBS):
- Prefix: `PREFIX = "session-pickup"` (L45), key `session-pickup/<owner>/<sid>/<epoch>/<series>/...` (`session_prefix` L245, `series_prefix` L249, `_key` L253: `t-NNNNNN[-sha].gz`, `code-*.bundle`, `side-*.tgz`). Docstring L13-15: lives OUTSIDE `<owner>/` because each agent runtime role reads `<owner>/*`. Also `COPIES_PREFIX="session-copies"`, `COPY_TTL_S=3600` (L48-49) for redacted link copies.
- Upload: Mac calls device-JWT routes in apps/backend/routers/desktop_session_pickup.py (authorize via `authorize_device` L618-652: `machine_auth._device(token,"openclaw-macos")`, paired Mac owns the row, same human, `provider == "claude"`, `pickupAllowed`, not `unshared`; expiry retires). `presign_uploads` (L666-691): presigned `put_object` with `ContentLength` signed, `UPLOAD_URL_SECONDS=300` (L54), `URL_SECONDS=900` for GETs (L51); part caps `PART_CAPS` transcript 64 MiB / code 200 MiB / sidecars 200 MiB (L56), `SNAPSHOT_CAP` 1 GiB (L57), `MAX_PARTS=1000` (L60), append-only indexed parts with a 409 `_continuity` resync. `commit` (L694) writes the manifest row `PICKUP`.
- Mac-side signal: `PICKUPWANT#<device>#<sid>` on `LOCALUSER#<owner>#<human>` (`_want` L308-327), delivered through heartbeat ack (`ack_with_pickup` L598). A recording would need an equivalent (e.g. RECORDWANT) or just be always-on for shared sessions (INF).
- Retention: `PICKUP_RETENTION_SECONDS = 7*24*3600` (local_sessions.py:189); `pickup_stopped_at`/`pickup_expired` (L740-757) count from `endedAt` or a lapsed lease; sweeper `sweep_once` (session_pickup.py:854-885; interval `SWEEP_INTERVAL_S`=3600, `ABANDONED_UPLOAD_S`=3600, `retire` L888, runner L952). `_session_prefixes` lists S3 hourly (ponytail comment L841).
- IAM: apps/infra/lib/stacks/service-stack.ts:960-982: `SessionPickupRW` on `.../session-pickup/*` and `SessionCopiesRW` on `.../session-copies/*` (backend task role; comment: must stay OUTSIDE `<owner>/`). Agent runtime role reads `<owner>/*` (service-stack.ts:934 `*/hermes/*`; CLAUDE.md "OWNER_STATE_BUCKET"). A recording under `<owner>/` would be readable by agents -> must be a new backend-only prefix (e.g. `session-recordings/<owner>/<sid>/`) plus a new IAM statement mirroring SessionPickupRW (INF).
- Precedent `browser-state/<owner>/...`: browser_extensions.py:98-106 (`object_key`, `helper_key`, `policy_key`), IAM service-stack.ts:936,950; comment "agent's runtime can read AND write <owner>/*".
- Existing frames are 11 KiB raw each, 2 MiB/min/session budget, snapshot every 10 s only while watched (INF: an always-on recording must be taken Mac-side from the PTY output stream, not from `forward`, which drops frames over budget and has no viewer when nobody watches). Privacy: pick-up docs say transcript is unredacted and kept until 7 d after end; a terminal recording has the same exposure (INF).

#### 6. iPhone
- apps/mobile/src/app/(app)/session/[sessionId].tsx (572 lines, OBS). Outer `SessionScreen` (L55): `useWorkspaceSessions`, fallback `getLocalSession` fetch for ended sessions (L~85-125), live only when focused+AppState!="background" (L68-75). Inner pane (L~311-): `useHistorySource`, `useLocalSessionHistory` (pollMs), `useSessionCommand`, `useSessionResume`, `useSessionOutbox`; banners: waiting-on-owner banner (L~383), liveHint "Press <- in its terminal..." (L394-399), `SessionApprovalCard` (L409, claude + pendingApproval), `JoinRequests` (own); inverted FlatList transcript `testID="session-transcript"` (L412-) with WorkingRow/PendingPromptRow/queued outbox rows in header; footer area: `ResumeDelivery`, then `SessionResume` if ended, else `SessionPromptRow` if `control`, else `AskToJoin`; Details `BottomSheet` with `SessionOverview events={events}` (plan/changes). Components: apps/mobile/src/components/session/{SessionApprovalCard,SessionJoin,SessionOverview,SessionPromptRow,SessionRenameSheet,SessionResume,SessionToolIcon,TranscriptRow}.tsx.
- Expo `~57.0.4`, react-native `0.86.0`, react 19.2.3, `react-native-webview 13.16.1`, `expo-file-system ~57.0.0` (apps/mobile/package.json:29,60,68,38). No `expo-dom`, no `expo-asset`, no xterm in package.json; grep for `'use dom'` in apps/mobile/src and app: none (OBS). INF: expo-dom would be a new native-dependency -> changes the `fingerprint` runtimeVersion (CLAUDE.md "runtimeVersion.policy fingerprint"), so it needs a TestFlight build, not an OTA; same for any new native module. react-native-webview is already in the binary so a WebView route is OTA-safe.
- Only WebView use: apps/mobile/src/components/room/AttachmentViewerModal.tsx:19 import; L186 video `source={{ uri: videoUri }}` (presigned URL, direct); L210-214 `source={{ uri: file.uri }}` with `originWhitelist={["*"]}` for a downloaded local file URI (expo-file-system cache). No bundled local HTML asset anywhere (OBS: no *.html in package or metro config). xterm.js would have to be injected as an HTML string (`source={{ html }}`) with the xterm bundle inlined (no CDN needed offline) or written to a cache file; messaging via `postMessage`/`injectJavaScript`. (INF)
- iPhone input today: keyboard text via `SessionPromptRow` -> `useSessionCommand` (command route), not `/input`. A terminal view on phone needs `sendLocalSessionInput` (already in the shared client) and a soft-keyboard/key-row (Esc, Ctrl, arrows, Tab) UI (INF).

#### 7. Public session links Phase 2 (public Watch)
NOT implemented (OBS). grep for PUBWATCH / LINKVIEWER across apps and packages: zero hits. api-stack.ts has only the `ws` namespace (L669-677: publish IAM, subscribe OIDC only); no `pub` namespace, no `API_KEY` auth provider. No `/public/session-links/viewing` route (only `/public/session-links/events` L387 and open/copy). `AppSyncEventsClient` has no apiKey mode (INF from spec L322; not re-grepped). Phase 1 only: `/s` page polls events and renders the transcript (app/s/page.tsx:93,185). Spec §4 (L313-340) is design only.

#### 8. Agent skill text that would change (apps/infra/hermes/skills-src/lightbulb/references/surfaces.md, section "Shared local-session workspace notices" starts L91)
Sentences describing the human view (OBS):
- L96-101 "A running session shows its live terminal when Lightbulb started it, when it is a Claude Code background session, or when it is a plain Codex session ... A plain interactive Claude Code session (...) shows the labelled transcript."
- L112-120 "A running session shows as a live terminal. A live terminal has no Lightbulb message box ... A session that is not running (ended, or its Mac is offline) shows as a transcript, labelled "Transcript" in its status line. ... The transcript renders the native transcript with the harness's own grammar: prompts with the sender's name, tool calls with collapsed output previews, thinking markers, turn boundaries and a working line. Codex sessions read as `• Ran ...`; Claude sessions as `⏺ Bash(...)`. Updates arrive block by block, not token by token."
- L188 "Asking, allowing, declining and removing are logged in the transcript."
- L192-205 "it falls back to the transcript when the session is not running or has no frames" and "You read the session's structured record with `lightbulb_local_sessions` instead" (the second stays true; agents still read events).
- L113 mentions the "Watching . Ask to join" line (keep).
Also: SKILL.md:47 routing row (agents read recent tail via `lightbulb_local_sessions read`) STAYS accurate. MAINTENANCE.md anchors: L117-128 "Shared local sessions" and "Session Details dialog ... (status line, Details tabs, Access switches, footer)" L128 list NativeSessionTranscript.tsx; update those file anchors if the component goes. The desktop skill apps/desktop/src-tauri/skills/lightbulb-session-sharing/SKILL.md is also listed (L126) and may describe the same view (not read).
Other docs to revise: managed-session-sharing spec decisions #4, #8 (above), spec 2026-10-05 public links §2.1 L51-54/L64 (copy about terminal vs transcript).

### 1.5 Room model facts for session chat (subagent report, cited)
#### 0. Correction to the brief (OBSERVED)

There is NO "DM beside a session" today. "Message <owner>" REPLACES the middle:
- `onSelectParticipant: async (p) => { closeSession(); await selectParticipant(p); }`
  F/components/messaging/MessagingWorkspaceContext.tsx:770. `closeSession` = `setSessionFocus(null)` (:668).
- `MessagingConversation` renders exactly one of AllSessionsView / NewSessionView / SessionFocus / RoomPanel
  (F/components/messaging/MessagingWorkspace.tsx:46-62). SessionFocus is a single column
  (`.room-session-focus` = 1-col grid, F/components/messaging/messaging.css:460-462).
- Only ONE room is ever live in the context: one `useRooms(...)` (Context:179), one `activeRoomId`.
  `roomVisible = subscribeToRoom && !sessionFocus` (:171): a covered room neither auto-reads nor silences notifications.
- INFERRED: a side-by-side chat needs a second room "slot" (or generalizing `useRooms`), not just a layout change.

#### 1. Room model (backend)

| Fact | Where |
|---|---|
| kinds `channel`/`dm`/`group-dm`; roles owner/admin/member; listen modes active/mentions/observe | B/core/services/messaging.py:42-45 |
| `MAX_ROOM_MEMBERS = 32` | B/core/repositories/messaging_repo.py:65; enforced in service `create_room` :2121, repo `create_room` :713, repo `upsert_member` :1371-1374 |
| `RoomSummary` fields: roomId,name,kind,roomRevision,lastMessageSeq,lastMessageAt,lastMessage,unreadCount,mentionCount,updatedAt,starred,muted,sectionId,topic,members,lastReadSeq,workspaceId,shared,guest,isDefault,archivedAt/By | messaging.py:244-308 |
| `RoomDetail` adds deliveryKey, createdAt, pinnedMessageIds, activeRuns, readCursors, agentDeliveries | messaging.py:315-328 |
| Summary projection `_room_summary_fields` (no generic META passthrough: a new META key is invisible to clients until added here AND in RoomSummary) | messaging.py:1568-1592 |
| `topic`: validated `validate_room_topic` :985; PATCH owner/admin only (not DM) :2169 |
| Archive: owner/admin; group-dm members may archive; channel+group-dm only, never default/vendor (`_require_archivable`); create_message is fenced `attribute_not_exists(archivedAt)` | messaging.py:2169-2210; repo :1018 |

##### create_room and `extra`
- Service `create_room` (messaging.py:2116-2161): room id = `uuid5(owner|creator|clientRoomId)` (`deterministic_room_id`, repo :242); creator becomes role `owner`; DM dedupe via `_find_existing_dm` (:2090). It passes NO `extra`.
- Repo `create_room(..., extra: dict|None)` (repo :697-760): META is built with `**_ddb_safe(extra or {})` spread LAST (:739), so extra keys land top-level on META and can override base fields. One transaction: META + owner catalog row + IDEMP + member rows + agent delivery cursors.
- Only caller that passes `extra`: meeting_invites.py:307, `extra={"meeting": meeting, "hidden": True}` (Google Meet rooms).
- `hidden` rooms are filtered out of the sidebar list (`_visible_room_metas`, messaging.py:1666-1675) and `create_agent_message` treats them specially (:4186). Precedent for a "not in the sidebar" room.
- `_ROOM_META_FIELDS = ("name","topic","archivedAt","archivedBy","meeting")` (repo :1652) is the PATCH allowlist; a new META key would not be client-patchable (good).

##### Platform-managed / id-derived rooms (precedent)
- `default_room_id(owner)` = uuid5("lightbulb-default-room|owner") messaging.py:1754; `is_default_room` :1766 matches by ID, never name.
- `vendor_room_id` repo :248; `is_vendor_room` messaging.py:1759; `_deny_vendor_human_change` :1778 forbids human add/remove.
- `meeting_room_id(owner, uid)` repo :254, `client_room_id=f"meeting:{uid}"`.
- Seeding: `ensure_default_room` :1835 (creates once), `join_default_room` :1803 (AUTOJOIN# claim marker so a removed member is never re-seated; keeps marker if room full), `ensure_vendor_room` :1943 (caps at 32 with a metric, :2010-2014).
- A session room would copy this: `session_room_id(owner, sessionId)` = uuid5, `client_room_id=f"session:{sid}"`.

##### Membership rules (`upsert_member` messaging.py:2239-2313)
| Room kind | Who may add a member | Notes |
|---|---|---|
| channel | owner/admin only; any member may change an AGENT's listen mode | `mode_change`/`chat_addition` carve-outs |
| group-dm | ANY participant adds a workspace member, role forced `member` | `chat_addition` |
| dm | any participant; adding turns it into group-dm in place (`room_kind="group-dm"`) | repo upsert :1352 |
- Member must be in room-owner workspace (`_validate_human_in_owner` :1482); owner role immutable; new agent's delivery cursor starts at current lastSeq (no history replay).
- Remove: `remove_member` :2316 needs owner/admin (channel); `leave_room` :2330 (channel owner cannot leave; group-dm owner can); `delete_room` :2398 is channel-only tombstone, owner only, not default/vendor.

##### Revocation (security boundary)
- Human removal REQUIRES `new_delivery_key` + LIVE rows in the same transaction (repo `remove_member` :1533-1592). `_rotation_notices` (messaging.py:2372) writes `room.access.removed` to the removed human and `room.subscription.rotated` (with new key) to the rest. Old AppSync channel `/ws/<ws>/rooms/<room>/<oldKey>` goes silent.
- `delete_room` rotates and sends `room.access.removed` to everyone (:2398-2424). `revoke_human_membership` :2427 removes a departed org member from every room.
- `ensure_user_stream(owner, user)` (repo :2023) mints the user's private AppSync channel key; must run before adding a human (service does it in `create_room` :2133, `upsert_member` :2272, `join_default_room`).
- Reads re-check strongly: `require_room_member` :1519 (404 for strangers, 403+reconcile hint for ex-members).

#### 2. Agent members

##### `select_dispatch_targets` (messaging.py:3398-3432) OBSERVED
- skip source agent; `observe` NEVER runs (even if mentioned, even in a DM).
- mentioned agent OR `room_kind == "dm"` -> `user_request`.
- else `active` -> `room_event` (ambient).
- `mentions` mode: runs only when mentioned (or DM).
- If any `user_request` exists, all `room_event`s are dropped (:3425-3428).
- Note: `group-dm` is NOT addressed-by-construction (only `dm`). Agents in a group-dm need @mention/name-address/Jev.
- Before targets: `address(...)` (name-opening "Hey Friday, ...") runs for non-DM rooms (:3527-3535); Jev then picks ONE responder for plain messages among `active` agents; silence default for a message opening with a human @mention (spec in CLAUDE.md; jev.CONFIDENT_P=0.7 jev.py:445).
- `AGENT_REPLY_CAP = 2` (:50): agent message at hop >= cap gets no targets (:4255), posts a muted system line "paused after 2 replies" (:4370-4378).
- Native/session-sourced posts only wake `user_request` targets (:3541-3543).

##### How an agent gets session context OBSERVED
- Every room turn: `live_sessions_for_room(owner, room, members)` (B/core/services/local_sessions.py:1089) -> `body["sessions"]` (openclaw_work.py:481-483, build body :256-259). Bounded 24; includes sessions SHARED by any HUMAN MEMBER of the room; returns [] if ANY member's home workspace differs (shared/vendor room).
- Chip refs: `sessionMentions` (max 4, CreateMessageRequest messaging.py:549) -> `referenced_sessions` (local_sessions.py:1118) -> `body["referencedSessions"]` (openclaw_work.py:262-272). Needs: not unshared, not background, session owner is a room human, no foreign members. Offline/ended resolve (reference, not wake).
- Ruling "a session chip is a reference": create_human_message comment :3480-3481, validates each id with `authorize_session(auth, None, id)` (:3492), rejects if owner not in room (:3499), and `unshared`/background -> 404.
- Agents read the session via the `lightbulb_local_sessions` tool using the exact id (comment openclaw_work.py:263-264).
- INFERRED: an agent in a session-bound room whose owner-human is a member gets that session in `sessions` automatically on every turn, with no chip needed.
- Agent authority over a session is separate: `authorize_session` with `AgentInvocation` requires a room grant (`require_caller` local_sessions.py:538-554: agent must be a room member, invocation unexpired, no foreign members); control only for the requester who is the session owner (:928-935). Teammate-control opt-ins never apply to agents (`may_control` docstring :769).
- Frontend chip plumbing: `sessionPermalink`/`parseSessionPermalink` F/components/messaging/messageLinks.ts:11-26; `linkifySessionMentions` used at RoomPanel.tsx:576. (I did not read the chip card component.)

#### 3. Notifications and unread

- Projector Lambda `apps/infra/lambda/messaging-projector/index.py`: `_notify_members` :737-800. Per current member (strong read): activity kind via `_activity_kind` :691 -> `dm` for ANY dm/group-dm message; `mention` if @mentioned; `thread_reply` if participant; else None (channel, unmentioned = NO notify, no push). `notify = seq > lastReadSeq and kind is not None`. Muted (`member.muted is True`) suppresses push only (~:783, the one server-side mute); room.message.available still publishes to the user channel.
- Push title via `_push_where` :517: "DM" / "#name" / name or "Group chat".
- RoomSummary unread = top-level msgs above read floor (cap 99), `mention_count` incl. thread replies (messaging.py:1561, summary fields).
- INFERRED: kind `channel` = quiet by default (mention-only notifications); kind `group-dm` = every message notifies every unread member, wrong for a side chat unless muted by default.
- Hidden rooms do not appear in `list_rooms`, so they contribute nothing to sidebar unread / title badge (`unreadTotal` Context:896 uses `rooms.rooms`). A session chat would need its own unread surface.
- Session attention: `_notify_join_request` local_sessions.py:2123 writes an activity row + `_session_notice` :2171 publishes `workspace.session.attention` {sessionId,state,push} on the owner's user channel + Expo push (data.source="session"). Answers go back via `_notify_join_answer` :2196.
- Web: MessageNotifications.tsx:87-95 shows it unless the session is the active+focused one; room messages are suppressed only when `roomVisible && activeRoomId === roomId` and window focused (:121-123).
- Opening: `openRoom` window event (Context:888-892): `detail.sessionId` -> `openSession`; `detail.roomId` -> `openNotificationTarget` (:879-886) which calls `closeSession()` first. INFERRED: a notification for the session chat room would currently CLOSE the session.

#### 4. Web layout

- Shell: dockview. One Messages panel (`MESSAGES_PANEL_ID`, panelRegistry.ts:209) hosts MessagingConversation. Programmatic splits exist only for Tasks and Browser (`position: {referencePanel: MESSAGES_PANEL_ID, direction: "right"}`, workspaceController.ts:~285-290; `disableDnd` WorkspaceShell.tsx:465). `PanelKind = chat|teams|control|file-viewer|browser|activity|mail|missing` (panelRegistry.ts:6). `MIN_ROOM_WIDTH=560`, `MIN_BROWSER_WIDTH=640`; too narrow -> folded as a tab (`refitBrowserSplit` :~305).
- Sessions: `openSessionPanel(id)` -> `describePanel("chat",{session})` retargets the SAME Messages panel (workspaceController.ts:433-436; panelRegistry.ts:232-234, 293-298). "A session is a view inside Messages, never its own dock tab".
- `openSession` (Context:683-686) -> `applySession` sets `sessionFocus={sessionId}` and clears thread/target (:668-679).
- Existing right-column hosts:
  1. RoomPanel's own `<aside className="room-context">` (RoomPanel.tsx:783 "Room details", :841 "Tasks"; thread too). CSS: >=1180px grid `minmax(0,1fr) 344px`; 860-1179 absolute 344px overlay; <860 full-width swap (messaging.css:363-390). `showContext` and `isNarrow` hide `.room-conversation`.
  2. `dockSide(column)` portals that aside into `useBrowserThreadHost()` (RoomPanel.tsx:377,682-684; BrowserDockPanel `browser-thread-slot`, workspaceController.ts:529-541) when the Browser panel is docked beside Messages.
  Neither is available while SessionFocus is shown (SessionFocus has no aside; RoomPanel is not mounted).
- SessionFocus (F/components/messaging/SessionFocus.tsx): `<section className="room-panel room-session-focus">` > `.room-session-main` (flex column) > `.room-context-header` (messaging.css:133) + `LocalSessionPanel`. LocalSessionPanel: status line, `.local-session-terminal` live terminal, `.local-session-scroll`/`.local-session-history` scroller, prompt textarea `.native-session-prompt-row` (LocalSessionPanel.tsx:205-278). Terminal control path is `command.canSend`; chat must never share that composer.
- RoomPanel is large and props-driven (`roomProps` from Context:~775-860: roomId, messages, composer, threadComposer, onSelectThread, ...), so it is reusable as a component, but its data comes from the single `useRooms` instance. `RoomTimeline`/`RoomComposer`/`RoomMessageItem` are separate files (messaging/), usable standalone only if given the same props.
- Thread: messages carry `threadRootMessageId`; main timeline filters to `null` roots (RoomPanel.tsx:~392); replies grouped in `repliesByRoot` (:~394-406); thread opens in the aside via `onSelectThread` (Context `threadId`).
- Phone (<600px): `.workspace-shell` becomes 1 column; rail+sidebar become a fixed drawer toggled by `data-nav-open` and `.workspace-phone-bar` (workspace.css:541-575; WorkspaceShell navOpen). Dock split is impossible at that width (canSplit needs >=1200px dock).
- INFERRED existing building blocks for "chat beside terminal": (i) `.room-has-context` grid (344px, overlay, full-width swap) pattern in messaging.css, (ii) browser dock split + `canSplitBrowser/refit` fold-to-tab logic. A session-chat column most cheaply copies pattern (i): inside SessionFocus, a 2-col grid `minmax(0,1fr) 344px` >=1180px; below that a header toggle that swaps the body (like Room details), which also covers phones.

#### 5. Mobile (M = apps/mobile/src)

- Routes: `app/(app)/room/[roomId]/{index,details,thread/[messageId]}.tsx`, `app/(app)/session/[sessionId].tsx`, `session/new`. Stack screens declared _layout.tsx:162-168.
- Room screen needs only `roomId` (`useLocalSearchParams<{roomId}>` room/[roomId]/index.tsx:82,89); loads `rooms.activeRoom ?? rooms.directoryRoomDetails.get(roomId)` (:148). INFERRED: a `hidden` room not in the list loads via selectRoom/GET /rooms/{id}; verify before relying on it.
- Existing door: session screen `messageOwner` (session/[sessionId].tsx:181-188): `rooms.selectDm({kind:"human", id: session.humanUserId}, {activate:false}).then(dm => router.push("/(app)/room/${dm}"))`; shown as "Message {ownerName}" banner (:519) and in Details. It already is a push onto the Stack, so back returns to the session. Pushing `/(app)/room/${sessionRoomId}` the same way works with no new screen.
- Room screen also exposes details (:427), thread push (:608), Live (:270-283: voice huddle; note Live ignores listen modes).
- INFERRED: on phone the natural shape is a header "Chat" button with unread badge on the session screen that pushes the room route (cheapest), instead of a split.

#### 6. Permissions split (B/core/services/local_sessions.py)

| Capability | Check | Line |
|---|---|---|
| Read session / events / hydration | `authorize_session(auth, None, id)`: caller workspace owner == row.ownerId; any WORKSPACE MEMBER; `unshared` reads as 404 to everyone but owner | 899-937 |
| Anyone-link reader (non-member) | `AUDIENCES = workspace|anyone`, `/s#token` | 2253-2279 |
| Command/control | `owner_only=True` -> `may_control(row, user)` = owner OR `team_control_on` OR `allowed_senders` | 769-772, 929-936 |
| Interrupt | owner, or team control (allowed senders cannot stop) | 1270-1278 |
| Private send approval | owner only | 1272-1275 |
| Team control opt-in | owner toggles `teamControl`; dies at `endedAt` | 759-761, 1855 |
| Ask to join | `request_join` (not owner, not already controller) -> joinRequests; `decide_join` owner only -> `allowedSenders`; `remove_sender` owner only | 1944-2015 |
| Agent | needs room grant; control only for the human requester who owns the session | 538-554, 928-935 |
| Frontend | `LocalSessionPanel` `control`/`own`/`command.canSend` | LocalSessionPanel.tsx:257,271 |

OBSERVED: nothing in room membership grants session control today. `authorize_session` with a `room_id` only adds membership of that room as an extra gate (:915-925), and without `room_id` ignores rooms entirely. Room roles (owner/admin/member) never appear in local_sessions control checks.

A session-chat door MUST:
1. Authorize reads with `authorize_session(auth, None, sid)` (never with the chat room as `room_id`), so a revoked/unshared session reads as 404 even to a current chat member.
2. Never call `submit_command`/`may_control` from chat; chat composer must post via `messaging.create_human_message`, not the session command path. Keep `session_mentions` as reference-only (messaging.py:3480).
3. Not add chat members to `allowedSenders` or flip `teamControl`; join approval stays on `request_join`/`decide_join`.
4. Derive membership from session read access, and re-check on each room read (a stale member must not read chat for an unshared session: INFERRED needs a hook on `set_sharing` unshare).
5. Be workspace-only: `has_foreign_members` blocks referencing sessions in shared rooms (messaging.py:3488; local_sessions.py:1099, `require_caller`), so keep the room out of the vendor-share path.

#### 7. Proposed shapes (INFERRED)

##### (a) Lazy platform-managed room per session
- `session_room_id(owner, sid)=uuid5("lightbulb-session-room|owner|sid")`; create on first open via a new endpoint `POST /local-sessions/{sid}/chat` guarded by `authorize_session(auth, None, sid)`; `create_room(..., kind="channel", name=<session title>, extra={"session": {"sessionId": sid, "ownerId": humanUserId}, "hidden": True})`.
- Session's owner = room `owner`; add agents the owner picks (or default CEO) as `mentions` (not `active`, so Jev/cost stay bounded).
- Auto-join on open: reuse the `join_default_room` pattern (AUTOJOIN claim marker, `ensure_user_stream`, `upsert_member`, `_emit_member_added`, `_publish_room_metadata`) for any reader of the session; a person who leaves/is removed is not re-seated.
- Needed changes: add `session` (or `sessionId`) to `RoomSummary/RoomDetail` + `_room_summary_fields`; `is_session_room(meta)` helper; deny human add/remove like `_deny_vendor_human_change` (membership derived from session access); keep `hidden` so the sidebar and unread total stay clean; a second room slot in the web context; notification routing for `openNotificationTarget` that opens the session with chat pane instead of `closeSession()`.
- Kind choice: `channel` => only @mentions notify (good); `group-dm` => every message notifies everyone (:697-698) and `dm` semantics address agents on every message (select_dispatch_targets), costly.
- Trade-offs / breakages:
  - 32-member cap (repo :65): a busy workspace session has silent failures; vendor room precedent truncates + metric (:2010). Cap surfaces as MEMBER_LIMIT (messaging.py:2121 and repo upsert :1374); join_default_room swallows and keeps the marker.
  - Revocation: unshare / owner leaves workspace does not remove chat members automatically; must call `_remove_member_with_notices` (rotates delivery key) from `set_sharing(unshared)` and member-revoke paths. Without it chat history of an unshared session stays readable.
  - Ended sessions: room should stay readable (history ask) but `archivedAt` on session end (fence on create_message) gives a clean read-only state; archive is currently rejected for default/vendor/dm only, so a `channel` can be archived programmatically (reuse `patch_room_meta`, not the owner/admin gate).
  - Agent cost: any `active` agent in it runs on every message; keep `mentions`.
  - Search indexes it (messaging-search-indexer) unless `hidden` handling excludes it: not verified.
  - `delete_room` refuses default/vendor only; make session rooms undeletable (new guard).

##### (b) Reuse the sharer DM
- No new room, no cap, no lifecycle code. Already there: web `onMessageOwner` -> `selectParticipant`; mobile `messageOwner` push.
- Breaks: DM is private to two people, so teammates other than the owner can't see each other's messages; the owner's own session has no counterpart; DM is "addressed by construction" so agents aren't involved unless added (turns into group-dm = loud notifications for all); DM history mixes unrelated chatter; the DM is not session-scoped (no revocation tie to the session, no per-session history); and on web it closes the session (:770), so no side-by-side without the new second-room slot anyway.
- Conclusion: (b) is only a stopgap button; (a) is the smallest shape that satisfies participants, agents, history, and revocation. Biggest hidden cost of (a) is the web single-room context (`useRooms` x1, `roomVisible` rule) rather than the backend.

## 2. Inferred

Owner ruling 2026-10-07, after reading the first draft: no pictures of windows and no stored
recordings. The agreed design keeps only real terminal processes (held, attach, mirror), keeps an
ended holder's final screen in memory while the app runs, and shows an explicit state otherwise.
The two bullets below about recordings and window capture record the inference that led to the
first draft; they are superseded by that ruling.

- Lightbulb's holder already is herdr's server-owned pane; the gap is coverage (sessions we did
  not start) and persistence (history, recordings), not architecture.
- Origins with no attachable pty (IDE panels, Codex desktop app, Claude Desktop Code tab) can only
  show a real picture through a window capture; everything else can show real text (held,
  attach, or a host-terminal screen read).
- Ended, offline and resumed sessions cannot show a terminal without a recording. The 2026-10-03
  "no recording" ruling and the brief cannot both hold; the spec puts it to the owner as D1.
- The web viewer's `scrollback: 0` plus a reset on every snapshot is why viewers have no history;
  keeping scrollback needs only a client rule change plus a `history` frame kind.
- Concurrent typing: raw keystrokes interleave on one prompt; a Line mode (one atomic frame) is
  the smallest change that keeps drafts private without any typing pause.
- Session chat: a lazily created hidden channel per session, membership derived from session read
  access, is smaller and safer than reusing the DM; the hidden cost is the web context's single
  room slot.
- Browsers: a session's own browser is discoverable from its process tree only when an MCP
  browser server started a Chrome with a debugging port; the Claude-in-Chrome extension's
  everyday Chrome is not viewable without a window mirror.

## 3. Locally tested (founder's Mac, read-only probes, 2026-10-07)
- `claude --version` 2.1.292; `claude attach --help` attaches background sessions only;
  `--remote-control` flag present. `codex --version` 0.159.3 with `resume --remote`, `--no-daemon`,
  `remote-control` (experimental).
- `claude agents --json`: 19 sessions, all `interactive`, each on a `ttysNNN`; rows carry no
  `entrypoint` field on this version.
- Installed: VS Code (Claude Code extension 2.1.289/290/292), Cursor, ChatGPT.app, Claude.app,
  tmux; absent: it2, kitty, wezterm, agent-browser. Terminal.app AppleScript probe timed out
  (-1712). Running browser automation: `chrome-devtools-mcp@1.8.0`, `@playwright/mcp`.
- graphify: `graphify update . --no-cluster` via the post-merge hook finished at 0331385bd; graph
  queries and `explain` located `LocalSessionPanel`, `SessionTerminalView`, `holder_for_session`,
  `create_room` call sites.
- Nothing was built, run or changed on the Mac beyond the research worktree.

## 3b. Host spike: holder vs tmux vs herdr (locally tested, founder's Mac, 2026-10-07)

Owner asked whether tmux or herdr is a better host than the holder. Throwaway harness in the
session scratchpad (a Python client speaking the holder wire protocol, a headless xterm.js 5
renderer, a private tmux 3.6b server with hidden chrome, pty clients). Same Claude Code 2.1.292
`--model haiku` session under each host, same keystrokes. Nothing was built; the holder came from
the installed app 0.4.606 (`lightbulb-desktop holder <sock> --size=120x40 -- claude`).

**Rendering fidelity (holder).** Twelve snapshots over a run with a trust prompt, a short
answer, a long answer interrupted by Esc, a resize to 90x30 and `/exit`. For each snapshot the
raw pty bytes up to that offset and the holder's `vt100` snapshot were both rendered by headless
xterm.js and compared row by row: **0 differing rows in 12 of 12 snapshots**, including the two
after the resize. The `vt100` model reproduced Claude Code's screen exactly in this run. Not
covered: kitty keyboard mode, synchronized output, images, a Codex session.

**Esc.** Holder: Esc during a running answer → "Interrupted" drawn 0.11 s later. Esc during the
thinking phase (both hosts) cancels the turn and Claude puts the prompt back in the composer with
no "Interrupted" text. tmux with `escape-time 0` interrupted as well (seen on the next snapshot;
not byte-timed). herdr's docs set no Esc delay issue; untested.

**Many clients.** Holder (cheap shell program, no model): a `join` client never resizes (its
RESIZE is ignored, the program stays 40x120); a replacing interactive client sends DETACHED to the
previous one and resizes the pty (program sees 30x90), watchers and joined clients receive RESIZE,
and the joined client is PROMOTED when the replacer leaves; `replayFrom: 0` and REPLAY(0) return the
whole log (3,043 bytes). All as `holder/protocol.rs` and `run.rs` document. tmux with
`window-size latest`: when a 90x30 client attached beside the 120x40 one, the window shrank to
90x30 and the 120x40 client saw the small window inside a field of `·` dots. tmux's default
(`smallest`) shrinks the owner's own window instead. Either way it is one size per session, and
tmux's rendering of the mismatch is worse than the holder's scaling.

**Resources, scenario run (~60 s).** holder RSS mean 9.0 MB, max 10.3 MB, CPU mean 0.1 %;
tmux server RSS mean 3.3 MB, max 3.9 MB, CPU mean 0.1 %. Both negligible.

**20-minute idle run with four prompts.** Both hosts answered all four prompts (holder output checked for ONE, TWO, THREE, FOUR; the tmux session ran to its `/exit`). holder RSS mean 7.3 MB, max 9.5 MB, CPU mean 0.01%, max 1.1% (116 samples); tmux server RSS mean 1.0 MB, max 3.6 MB, CPU mean 0.01%, max 1.1% (122 samples). Neither process died, restarted or leaked over the 21 minutes.

**Observed side effect.** The spike sessions were plain `claude` processes, so the running
Lightbulb app discovered them and the mod band read "Shared as a transcript" inside the
tmux-hosted session (visible in the client capture). They ended with `/exit`.

**herdr (read, not run).** herdr is Rust + `portable-pty` 0.9 (vendored, the same crate the
holder uses) + `ghostty-vt` (Ghostty's terminal model through bindings). Its public socket API
gives clients `pane.read` (recent lines), `pane.wait_for_output`, `pane.output_matched`,
`pane.scroll_changed`, `session.snapshot` and agent state; attached clients use an internal
screen codec. Streaming a herdr pane to our web and phone viewers would mean polling `pane.read`
(the mirror shape) or speaking its private client protocol. Its agent state comes from the
foreground process plus "screen manifests" (regex over the bottom of the screen), with
`blocked` deliberately strict; we already get the same from hooks.

**Conclusion.** The holder is the same architecture herdr chose (server-owned pty with
`portable-pty`); the only component where herdr is stronger is the screen model (`ghostty-vt`
vs `vt100`), and in this run `vt100` showed no error. tmux adds a bundled binary, hidden chrome,
an escape-delay setting and worse multi-size behaviour for the same one-size semantics. Keep the
holder. If a later acceptance run finds a rendering error, swap the model crate (`ghostty-vt`
is the proven candidate), not the host.

## 4. Production-tested
None by this research. Prior prod results reused (memory, 2026-10-04/05): held sessions,
`claude --bg` attach, plain Codex attach (three holder closures in the first minutes, then
stable), the mod band with Allow/Decline/Pause and remote typing, session links Phase 1.

## 5. Matrix
See the plan's "Pass / fail / blocked matrix". Summary: real terminal PASSES for held, attach
(Claude background, Codex daemon) running sessions on web/desktop; FAILS for plain foreground
Claude with the switch off, `codex --no-daemon`, IDE and desktop-app sessions, ended, offline,
resumed-before-draw, viewer history, and the whole iPhone; the 30 s automatic transcript fallback
exists and must go; session chat does not exist; browser items are research-only and last.
