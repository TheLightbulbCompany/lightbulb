# Managed session sharing

Status: shipped 2026-10-03 (PR #3655). This is revision 4: it describes what was built. Where an
earlier revision and the code differ, the code won. Mockups: "Session Sharing Screens",
https://claude.ai/artifact/Jf4r2sd92ubE3pcgG9BnHZ (private to the owner).

## Changes since revision 3

- The live terminal is inside the app's session panel. There is no separate terminal window.
- A session ends when its provider exits or the owner uses End in the panel. Closing or hiding
  the panel, quitting the app and updating the app do not end it.
- Teammates the owner admitted can type into the live terminal. Everyone else who can read the
  session watches read-only. Revision 3 said nobody remote types.
- The first frame arrives in about a second (`local.session.watch` nudge).
- `lightbulb claude` and `lightbulb codex` pass all provider flags through, except non-interactive
  forms. When a terminal holds a session, the panel joins it instead of taking it over.
- Resume rides a `resume` attribute on the session row plus a `RESUMEWANT#` row. An idle Mac
  sends a heartbeat every 30 s.
- Open questions 2 and 4 are answered (§11). The kill-test section is now a short list of what
  was verified (§10).
- The relay connection fix (#3652) shipped in the same train (§7).
- One-off repair scripts are not in the repo. Ending the sessions that already existed without a
  holder is a manual step after deploy (§8).


## 1. Problem

Shared Claude Code / Codex sessions today are found by discovery (every CLI process on a
paired Mac), mirrored from their transcript files, and redrawn on web and phone as an
imitation of each terminal:

- **It is not where you code.** The desktop app is a delayed viewer. The owner wants to
  "feel comfortable using the desktop app for my coding".
- **It does not look like the real thing.** The transcript has no partial text, spinner or
  status line, and every Claude Code / Codex release changes the look we imitate.
- **It falls behind.** On 2026-10-02 a busy session's panel was ~5 hours stale
  (`Pondering… (19179s)`): one upload batch per 500 ms across every mirrored session, and
  each refill read ≤32 rows from every open subagent before the main thread. Draft PR #3614
  fixes this and is Phase 0.


## 2. Decisions (owner, 2026-10-02 and 2026-10-03)

1. **Sharing is explicit, by how a session starts.** Shared if started from inside the
   Lightbulb desktop app or with the `lightbulb` command (`lightbulb claude`, `lightbulb codex`).
   Plain `claude` / `codex` is private. No per-session switch, no setting, no shim.
2. **No SDK, no headless mode.** The real, unmodified Claude Code / Codex terminal program runs
   in a pseudo-terminal (pty) that Lightbulb holds.
3. **The owner codes in the live terminal inside the app's session panel**, after clicking it.
   The same session can also be run from any terminal with `lightbulb claude`.
4. **Everyone who can read the session watches the real terminal while it runs.** Nothing is
   recorded or stored. A session that is not running shows the existing session view.
5. **Teammates the owner admitted can type.** That means people in `allowedSenders` (Ask to join)
   or with team control. Their keystrokes act as the owner on the owner's Mac. Everyone else
   watches read-only. The message box stays under the terminal.
6. **A session ends** when the provider exits or the owner uses End in the panel (two clicks,
   inside the local terminal page).
7. **Resume:** the owner and teammates allowed to send can resume an ended session, including
   from the iPhone.
8. **The phone keeps the session view** (the structured record), plus Resume. It has no terminal.
9. **Agents never get the screen or input.** Room-scoped and MCP callers do not either.

Sessions that cannot be started this way are not shareable: the Codex desktop app
(`ChatGPT.app`), IDE extensions, and Claude Code background sessions.


## 3. What this supersedes

The parity plan and the one-MCP spec must be edited to point here:

- `docs/superpowers/plans/2026-10-01-native-session-parity.md` line 7 ("Discovery remains the
  default … Managed launch is paused"), B4 and line 115 ("no opt-in controlled mode"):
  Lightbulb now hosts the native client. It still never drives it headless.
- Same file, line 113 ("workspace reading is shared by default"): still workspace-wide, but
  only for sessions started through Lightbulb.
- `docs/superpowers/specs/2026-10-02-one-lightbulb-mcp.md`, "Mac with the Lightbulb app,
  sharing enabled": becomes "a session started through Lightbulb".
- `core/services/local_session_relay.py:679-682`, "a restarted process is a new run the owner
  has not opted into": Resume is the exception, and only for the person who resumed it
  (§5.7).

Kept: parity plan B5 ("Never … turn teammate text into owner input"); the
`PermissionRequest` hook as the only remote way to answer a prompt; owner-only sending unless
the owner admits someone (Ask to join, team control); desktop consent given once at pairing;
pause sharing (`set_sharing`) for a running shared session.

## 4. What people see

| Who | In the Lightbulb app | Outside the app |
|---|---|---|
| Owner, started in the app | The live terminal in the session panel. New session shows it at once. | None |
| Owner, started with `lightbulb claude` | The session is listed. Opening it makes the panel join the terminal's session (§5.3). | The real terminal, unchanged, after one "Shared with <workspace>" line |
| Owner, plain `claude` | Nothing | The real terminal, private |
| Teammate (web, desktop), running session | The real terminal, live, with the message box under it. Read-only, unless the owner admitted them: then they can type. | None |
| Teammate, session not running (ended, or the owner's Mac or app is off) | The existing session view, and Resume | None |
| Owner or teammate on the phone | The session view, approval cards, message box, Resume | None |
| Agents (Friday) | The structured record, as today. Never the screen. | None |

Remote start, pick-up and Resume start a held session with no window and post a native
notification. The panel attaches when someone opens the session.


## 5. Design

```
 owner's Mac
 +-------------------------------------------------+
 | holder (one per session)                        |
 |   pty -- claude / codex                         |
 |   terminal model: screen, modes, probe answers  |
 |   output log with byte offsets                  |
 |   unix socket: hello, output, input, resize     |
 +------^------------------------^-----------------+
        | one interactive client | watch / type clients
        | (+ joined clients)     | (live relay, panel)
 `lightbulb claude`          Lightbulb desktop app
                             +- panel: child webview, local assets
                             +- live relay: snapshot + output -> backend -> viewers' user channels
                             +- remote input: backend -> `type` client (admitted typists)
                             +- existing mirror (hooks, transcript) -> EVENT# rows
```

### 5.1 The holder

One process per shared session, started by the desktop app. It owns the pty and nothing else: no
network, no auth, no session bookkeeping. Code: `apps/desktop/src-tauri/src/holder/`
(`protocol.rs`, `model.rs`, `run.rs`, `manager.rs`, `panel.rs`).

- **Process.** The holder is not the app's child, so it survives the app quitting or updating.
  It exits when its direct child (the provider, or the shell that `exec`s it) exits.
- **Environment.** The holder reads the user's login-shell environment once and starts the
  provider with it. It sets `TERM=xterm-256color` and `COLORTERM=truecolor`.
- **Terminal model.** A `vt100` terminal model tracks screen and modes, sends a **snapshot** on
  attach, and answers terminal probes itself. It drops probe replies from clients, so two
  clients never both answer.
- **Frames.** `[u8 kind][u32 BE len][payload]`. Kinds: HELLO=1, OUTPUT=2, INPUT=3, RESIZE=4,
  EXIT=5, REPLAY=6, GAP=7, SNAPSHOT=8, DETACHED=9, PROMOTED=10. A client may ask for replay from
  an offset and gets the bytes or a GAP.
- **Client roles.** HELLO names the role: `interactive`, `watch` or `type`. One interactive
  client at a time. Watchers and typists attach beside it. A client that sends `join` never
  takes the keyboard from another interactive client (§5.3).
- **Discovery of holders.** No manifest. The app lists sockets in
  `/private/tmp/lightbulb-sessions-<uid>/h/<8hex>.sock`, connects and reads HELLO. Every
  connection checks the peer uid. Frame kinds 1-9 did not change when later roles were added;
  every addition is a HELLO or EXIT JSON field.

### 5.2 Desktop app

- **The panel terminal** (`holder/panel.rs`). The main webview loads
  `https://thelightbulb.company`, so the terminal is not part of it. It is a Tauri child webview
  of the main window (`Window::add_child`, the `unstable` feature), loaded from the app's own
  loopback server with vendored xterm.js (`apps/desktop/src-tauri/terminal/`) and placed over the
  rect the session page reports. It talks to its holder over a WebSocket that needs a
  single-use key, minted by the app and written only into that child's URL. The remote page
  never sees the key, and no Tauri command carries terminal input. A compromised page can move,
  hide or switch the terminal in front of the owner and nothing more. **End** is a control in
  the child page, two clicks.
- **Live relay.** Runs while the backend says someone watches a session. It sends the holder's
  snapshot, then output in chunks of at most 11 KiB raw, with `cols` and `rows`, as
  `local.session.screen`. Screen frames are fire-and-forget sends from a queue 8 deep. Nothing
  is kept.
- **Remote input** (`local_sessions/remote_input.rs`). A `local.session.input` frame goes to a
  `type` client on the holder. Terminal probe replies are stripped first, so a viewer's xterm.js
  cannot type answers to terminal queries into the provider. Input is dropped unless the
  session is watched at this generation and the current epoch. Known cost: Shift/Ctrl+F3 reads
  as a cursor-position reply and is dropped.
- **Session to holder mapping** (`local_sessions/screen.rs`): by the `holder` id that discovery
  puts on the mirror. The panel, the screen relay and remote input all use it.
- **Start paths.** Every launch starts a holder through one function, `start_held`
  (`session_start.rs`): New session in the app (`terminal_start`), remote start, pick-up and
  Resume. `cli_start` starts one for the CLI. None opens a window.

### 5.3 The `lightbulb` command

`lightbulb-desktop cli` (`apps/desktop/src-tauri/src/cli.rs`). The app installs a symlink
`~/.local/bin/lightbulb`. `lightbulb claude [args]` / `lightbulb codex [args]` asks the running
app over a uid-checked request socket to start a held session in the current folder, prints
"Shared with <workspace>", then attaches as the interactive client at the terminal's size.

- **Checks** (`session_start::cli_start`): the tool is installed, the folder is approved or
  inside an approved one, sharing is on, and the app is running. If the app is not running it
  prints "Lightbulb is not running" and exits 1. It never starts an unshared session.
- **Flags.** All provider flags pass through. Non-interactive forms are refused with exit 2:
  `-p` / `--print`, informational flags, and Codex non-TUI subcommands such as `codex exec`.
- **Exit code.** The provider's exit code is returned.
- **Joining.** When a terminal holds the session, the panel joins it (HELLO `join`). It watches
  and types at the terminal's size and its resizes are ignored. When the terminal leaves, the
  holder sends PROMOTED (10) and the panel becomes the interactive client. The CLI prints a
  "moved" line and exits 0 if the session moves to the app. Closing the terminal leaves the
  session running.

### 5.4 What counts as shared

A session is shared only if a holder runs it (`local_sessions/` discovery):

- **Claude:** discovery keeps a session only when its provider is the holder's direct child, or
  runs through `/bin/sh ... exec` (the CLI path).
- **Codex:** bound by the thread id its holder's `SessionStart` hook recorded
  (`h/<id>.thread`). Codex mirrors are filtered by that thread. `/new` rebinds.
- **The hook** of a private session exits at once, so private prompts get no `PermissionRequest`
  wait and no tool input reaches the app.
- **Server side:** `local.session.register` requires `holderId`, which every register path
  sends from the persisted mirror. The background kind is deleted.

Identity is unchanged: session id = uuid5(owner, device, provider, profile, conversation);
generation = the provider process's pid + birth.

### 5.5 Watching and typing

Running means the holder is alive and the owner's app is connected.

- **Viewing.** A viewer sends `POST /messaging/sessions/{id}/viewing` every 15 s. It writes a
  `VIEWER#<user>` row with a 45 s TTL and a `WATCH#<device>#<sid>` row (code:
  `apps/backend/core/services/session_screen.py`). The same call nudges the Mac with
  `local.session.watch`, only for desktops that advertise `liveTerminal`, so the first frame
  arrives in about a second. The device-heartbeat ack's `watching` list is the backstop.
- **Frames.** `local.session.screen {sessionId, generation, cols, rows, kind, data}`. The backend
  stores nothing and does not write the session META row. It checks the row and publishes to each
  current viewer's user channel. The Mac sends a fresh snapshot when the viewer set gains a
  member. A frame budget of 2 MiB per minute per session applies.
- **Viewer.** `apps/frontend/src/components/messaging/SessionTerminalView.tsx`. A typing
  indicator shows when an admitted person types.
- **Typing.** Who may type: the owner, and people admitted through `allowedSenders` or team
  control. Calls are limited to 4096 raw bytes each, 20 calls per second (burst 40) and 64 KiB
  per minute. The backend records who typed and when, as a burst row (`TYPED#`, 30 days), and
  never the keystrokes. Removing a person or pausing sharing cuts input at once.
- **Checks on every call.** Each `/viewing` and input call checks live workspace membership,
  because a removed member's Clerk token stays valid until it expires. Only a first-party Clerk
  session token (claim `sid`, `core/auth.py`) may type, watch or resume. An OAuth access token
  cannot.
- **Not running:** the client shows the session view with "Ended" or "Owner's Mac is offline",
  and Resume for an ended session.
- **Refused callers:** agents, room-scoped and machine/MCP callers get no screen and no input.

### 5.6 The structured record

Unchanged mechanism (hooks + transcript tail + `EVENT#` rows), restricted to held sessions. It
feeds the phone, agents (`lightbulb_local_sessions`), room @session, approval cards, delivery
receipts and offline send.

### 5.7 Resume

Code: `apps/backend/core/services/session_resume.py`, desktop `local_sessions/resume.rs`,
frontend `SessionResume.tsx`.

- **Who:** the owner, and humans who were allowed to send when the session ended
  (`resumeAllowed`, copied when the session ends). The owner can revoke it. Agents cannot
  resume.
- **Request:** `POST /messaging/sessions/{id}/resume {resumeId, message?}`. The waiting request
  is a `resume` attribute on the session META row, so one request per session is decided with
  the row's revision. A `RESUMEWANT#<device>#<sid>` row is the Mac's copy, delivered in the
  device-heartbeat ack. Both expire after 24 h. A Mac that sleeps starts the resume when it
  wakes.
- **Idle heartbeat.** An idle Mac sends an empty heartbeat every 30 s, so the ack can carry a
  want.
- **Guards:** `submissions_enabled`; the same Mac and provider binary; the folder from the
  session's own transcript or rollout must still be approved at resume time. The owner resuming
  gets the recorded mode capped at `default`. Anyone else gets plan mode. Open question 2 is
  answered "no extra safeguards" (§11).
- **Run:** the Mac refuses with "Not resumed: it is already running on <Mac>" if a held session
  owns the conversation. Otherwise it starts `claude --resume <id>` or `codex --no-daemon ...
  resume <thread>` in a holder as the holder's direct child, with no window, and posts a native
  notification "<Name> resumed <title>". The first new-generation registration consumes the
  waiting resume. A same-generation re-register does not. The generation-chain check is skipped
  for ended sessions.
- **First message:** the backend seeds the resumer into `allowedSenders` when the successor
  registers, then admits the first message as a normal send. A send whose outcome is lost
  shows as "unknown", never sent twice.

### 5.8 Phone

No terminal view. The session view, approval cards and message box as today, plus Resume.


## 6. Security and privacy

- **The remote page cannot type.** Only the local child webview types for the owner, and its key
  is single-use and never reaches the page (§5.2).
- **Remote keystrokes are limited.** Only admitted teammates can type, only into a running,
  watched session, only with a first-party Clerk session token, and only while they are still in
  the workspace. Their keystrokes act as the owner on the owner's Mac, so admitting someone gives
  that power. Pausing sharing or removing the person cuts it at once. Probe replies from their
  terminal are stripped. Prompts are still answered by the `PermissionRequest` hook for the exact
  tool call, and text sent through the message box keeps its label.
- **Resume by a teammate** starts a program on the owner's Mac. Bounds: only people allowed to
  send, the same folder (approved at resume time) and binary, plan mode, a native notification,
  and the Mac's consent switch.
- **The live view is unredacted.** It shows anything on screen while someone watches. Accepted
  because sharing is explicit. Humans only, never agents. Nothing is stored, and nothing is sent
  while sharing is paused or nobody watches. Typing is logged as who and when, not what.
- **Local sockets** are uid-checked, including the CLI request socket.
- **Terms.** The unmodified Claude Code binary runs with the user's own login, which
  Anthropic's legal page does not restrict ("Nor does it prevent an end user from signing in
  to the unmodified Claude Code binary with their own Claude subscription").


## 7. Failure modes

| Event | Result |
|---|---|
| App restarts, crashes or updates | Holders keep running. The panel reattaches from a snapshot. Viewers see "Owner's Mac is offline" until the app is back. |
| Panel closed or hidden | The session keeps running. |
| Holder crashes | The session ends, as if its terminal crashed. |
| Outside terminal closed | The CLI detaches. The session keeps running and stays in the app. |
| Mac sleeps | Viewers see "Owner's Mac is offline" and the session view. A waiting Resume starts on wake. |
| Relay down | Live frames are dropped. Viewers get a fresh snapshot when it returns. |
| Old desktop build | Its registrations are refused after the release. Its sessions are not shared. |

**Relay connection fix (#3652).** The desktop relay used to drop with "session acknowledgment
timed out". Desktop `local_sessions/transport.rs` now has a deadline arm (`next_or_deadline`)
that lets an answer already in the socket win. It accepts a late ack and keeps the clock anchor
(`ack_anchor`). It runs discovery in its own task and moves blocking work off the connection
loop. Backend `routers/websocket_chat.py` answers `INTERNAL` to the frame the desktop waits on
(register, heartbeat, ended) after an unexpected exception, so the desktop frees its slot and
retries. Backend `core/services/local_session_relay.py` retries only the session row that lost
its revision race, not the whole heartbeat.


## 8. Migration

The backend refuses registrations without `holderId`. The desktop app shows once: "Sessions are
now shared only when you start them in Lightbulb or with `lightbulb claude`." No flag and no
compatibility path (owner ruling, 2026-09-12).

Ending the sessions that already existed without a holder is a manual step. The repair script is
a one-off kept outside the repo. After the backend deploys in each environment (dev first, then
prod): copy it into the backend task, run it as a dry run and check the list, then run it with
`--apply`. Ended sessions start their pick-up retention (`pickup_stopped_at`).


## 9. Later, not in this spec

Remote Stop (fixed `Esc`), slash commands from remote surfaces, agents reading the current
screen as text, a terminal view on the phone, Windows and Linux.


## 10. What was verified

Unverified here: nothing below was re-run for this revision.

- The Phase 1a holder and xterm.js tests in §12 passed as written there.
- The combined PR (#3655) merges five PRs with real merges. Its description lists the merge
  conflicts it resolved and what it checked in the merged code: every launch path starts a
  holder and opens no window, and CLI and Resume sessions pass the sharing rule.
- CI runs `tests/holder.rs` and `tests/cli.rs` for the holder and the CLI.
- The PR description lists prod acceptance steps (panel, watch and type, relay, plain Claude
  private, held Claude shared, Codex in a holder, Resume for owner and teammate, resume refused,
  CLI). They are for the founder to tick after promotion. This spec does not claim they passed.
- Not verified: update survival of a holder started by the shipped app, Keychain and macOS
  identity behaviour for a held `claude`, and an 8-hour memory run.


## 11. Open questions

1. Answered 2026-10-03: the session ends when the provider exits or the owner uses End in the
   panel. Closing or hiding the panel, app quit and app update do not end it.
2. Answered 2026-10-03: a teammate's Resume needs no extra safeguards when the owner's Mac is
   locked or the owner is away.
3. Answered 2026-10-03: no founder-only build; Phase 2 shipped to everyone.
4. Answered 2026-10-03: everyone who can read the session may watch. Admitted teammates may
   type.


## 12. Evidence

Live tests on the founder's Mac, 2026-10-02 (Claude Code 2.1.287/288, Codex 0.159.3):

- A real `claude` in a pty owned by another process registered as an ordinary interactive
  session and was mirrored unchanged.
- A prototype pass-through relay: correct width, resize followed, slash commands by keystroke
  ran (and saved `/model` and `/effort` as the user's defaults), an injected `Esc` stopped a
  turn cleanly, ~0.9 KB/s during a reply. Tested against a simulated terminal only.
- `SIGINT` to an interactive `claude` closes the whole session.
- `claude --resume <id>` from a new process kept the conversation.

Phase 1a, task 1.1 (holder prototype, `spike/managed-sessions`, Claude Code 2.1.288, `vt100`
0.16, `portable-pty` 0.9):

- Claude started with **no client attached** came up normally; a client attaching 15 s later
  got a correct snapshot, including bracketed-paste mode (`?2004h`).
- The only terminal queries Claude sent were the kitty keyboard query and DA1
  (`ESC[?u ESC[c`); answering DA1 alone is enough. `vt100` does not track kitty keyboard or
  synchronized output; nothing visibly broke without them.
- A client killed mid-turn and a new one attached: same provider pid, the snapshot showed the
  turn's output.
- A three-line bracketed paste at idle stayed in the prompt (status stayed idle).
- `Esc` with a permission prompt open rejected it; the file was not written. `Esc` during a
  turn was inconclusive this run (the turn finished first); it interrupted cleanly in the
  earlier pass-through test.
- Output volume: 47 KB over 122 s (~0.4 KB/s).
- Claude's normal "trust this folder" question appears for a new folder, as in any terminal.

Phase 1a, task 1.2 (xterm.js 5.5 in headless WebKit through Playwright 1.63, against the held
session; Safari's engine without driving the owner's screen):

- Renders correctly at 136x45; resizing to 93x33 redrew once, no duplicated prompt box.
- **Shift+Enter submitted the prompt** (xterm.js sends `CR`). Option+Enter (`ESC CR`) inserted a
  newline. Fix: map Shift+Enter to `ESC CR` in the terminal page.
- Option+B moved back one word (`macOptionIsMeta`).
- A three-line `term.paste` (the Cmd+V path) stayed in the prompt; status stayed idle.
- The `/` menu opened and filtered as typed.
- `Esc` during a running reply showed "Interrupted"; `/exit` ended the session and the holder.
- Not covered: OS clipboard Cmd+C/Cmd+V, image drop (needs the real app), 8-hour memory.

Reviews: desktop architecture, backend/security architecture, and an over-engineering pass
(2026-10-02); their blocking findings are resolved in this revision. Research: Happy Coder
source, Superset (PTYs held by a host service, headless emulator snapshots), VibeTunnel,
Anthropic's legal and Agent SDK pages.
