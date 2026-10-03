# Managed session sharing

Status: draft, revision 2 (after two architecture reviews and an over-engineering review),
2026-10-02. Mockups: "Session Sharing Screens",
https://claude.ai/artifact/Jf4r2sd92ubE3pcgG9BnHZ (private to the owner).

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

## 2. Decisions (owner, 2026-10-02)

1. **Sharing is explicit, by how a session starts.** Shared iff started from inside the
   Lightbulb desktop app or with the `lightbulb` command (`lightbulb claude`,
   `lightbulb codex`). Plain `claude` / `codex` is private. No per-session switch, no
   setting, no shim.
2. **No SDK, no headless mode.** The real, unmodified Claude Code / Codex terminal program
   runs in a pseudo-terminal (pty) that Lightbulb holds. Not Happy's kill-and-relaunch.
3. **The owner codes in a terminal panel inside the desktop app.** The same session can be
   attached from an outside terminal with `lightbulb claude`.
4. **Teammates see the same real view**, live while the session runs and recorded
   afterwards. The terminal output is recorded and stored, unredacted (accepted).
5. **Resume:** the owner and teammates allowed to send can resume an ended session,
   including from the iPhone.
6. **The phone keeps the session view** (the structured record), plus Resume.
7. **Nobody remote types into the terminal.** Teammates, the phone and agents contribute
   through today's labelled path (review finding, not contested).

Sessions that cannot be started this way are not shareable: the Codex desktop app
(`ChatGPT.app`), IDE extensions, and Claude Code background sessions. Codex desktop
sessions are shareable today and stop being so.

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
| Owner, started in the app | The real terminal in a panel | — |
| Owner, started with `lightbulb claude` | The session is listed; opening it moves the session's keyboard and size to the panel | The real terminal, unchanged, after one "Shared with <workspace>" line |
| Owner, plain `claude` | Nothing | The real terminal, private |
| Teammate (web, desktop) | The real terminal view: live, frozen with an "Offline" note while the Mac is away, recorded after it ends; message box and Resume under it | — |
| Owner or teammate on the phone | The session view, approval cards, message box, Resume | — |
| Agents (Friday) | The structured record, as today. Never the recording. | — |

## 5. Design

```
 owner's Mac
 ┌─────────────────────────────────────────────────┐
 │ holder (one per session)                        │
 │   pty ── login shell ── claude / codex          │
 │   terminal model: screen, modes, probe answers  │
 │   output log with byte offsets (ring, 1 MiB)    │
 │   unix socket: hello · attach · input · resize  │
 └──────▲──────────────────────▲───────────────────┘
        │ one interactive      │ recorder (output only)
        │ client at a time     │
 `lightbulb claude`      Lightbulb desktop app
 or the app's            ├─ terminal window (local assets, own IPC permission)
 terminal window         ├─ recorder ─ live chunks ──► relay ─► watching viewers' user channels
                         │           └ segments + keyframes ─► S3 (presigned PUT) + manifest row
                         └─ existing mirror (hooks, transcript) ─► relay ─► EVENT# rows
```

### 5.1 The holder

One process per shared session, started by the desktop app. It owns the pty and nothing
else: no network, no auth, no session bookkeeping.

- **Process.** Spawned double-forked and `setsid`, so it is not the app's child and survives
  the app quitting or updating. The pty is created with `portable-pty` (controlling terminal
  set). The holder exits when `waitpid` reports its direct child gone, not on pty EOF.
- **Binary.** Shipped in the bundle's resources and signed like the existing sidecars
  (`tauri.conf.json` `resources`). It runs from inside the bundle if kill test 1 shows the
  updater's bundle replacement does not kill it; only if that fails is it copied out.
- **Environment.** The holder reads the user's login-shell environment once
  (`$SHELL -l -i -c 'env -0'` with stdin closed and `DISABLE_AUTO_UPDATE=true`, as VS Code
  does), then starts the provider **directly** with it. Running the provider *through* an
  interactive shell is wrong: the shell's own startup prompts run inside the session (the spike
  hit oh-my-zsh's "Would you like to update? [Y/n]", which swallowed the first keystrokes).
  Starting it directly also makes the provider the holder's own child. The holder sets
  `TERM=xterm-256color`, `COLORTERM=truecolor`, and `LANG` when unset (a GUI app's
  environment has none).
- **Terminal model.** The holder feeds all output through an in-process terminal emulator
  (a Rust crate such as `alacritty_terminal` or `vt100`; Phase 1 picks one). It:
  - tracks screen, scrollback and modes (alt screen, bracketed paste `?2004`, focus `?1004`,
    mouse, cursor, synchronized output `?2026`, keyboard protocol);
  - on attach, sends a **snapshot** (serialized screen + the escape sequences that restore
    those modes) instead of raw history, so a freshly attached terminal pastes and encodes
    keys correctly;
  - **answers terminal probes itself** (DA1, XTVERSION, OSC 10/11 colours, kitty keyboard
    query) with one fixed xterm-256color profile and drops probe replies from clients, so a
    session with no window attached starts normally and two clients never both answer.
- **Output log.** Every output byte has a monotonic offset. The holder keeps the last 1 MiB.
  `output{offset, bytes}`; a client may ask `replay{fromOffset}`, answered with bytes from
  that offset or `gap{fromOffset, toOffset}` if they are gone.
- **Clients.** One interactive client at a time (the panel or `lightbulb claude`): the newest
  attach takes over keyboard and size and the previous one is told it was detached. The app's
  recorder attaches output-only.
- **Discovery of holders.** No manifest file. The app lists sockets in
  `/private/tmp/lightbulb-sessions-<uid>/h/<short id>.sock` (the directory the app already
  uses), connects, and reads `hello{protocol, pid, pidBirth, provider, cwd, providerPid,
  providerBirth, conversationId?}`. A socket that refuses connection is a dead holder; unlink
  it. Every connection checks the peer uid. If macOS's tmp cleaner is found to remove idle
  sockets, move them to Application Support (short id keeps the path under 104 bytes).
- **Protocol.** Version 1. `hello` carries the version; if a running holder speaks a version
  the app does not, the panel says "Restart this session to update it".

### 5.2 Desktop app

- **Holder manager** (Rust): starts holders (folder + provider + args), finds running ones,
  keeps the recorder attached to each, and exposes the list.
- **Terminal window.** xterm.js in its **own webview loaded from bundled local assets**, with
  a Tauri capability that is the only one allowed to call the holder commands. The main app
  webview is loaded from `https://thelightbulb.company` (`capabilities/default.json`); it may
  ask to open a terminal window but never send input. A bug or compromise in the web app
  therefore cannot type into a shell.
  The window maps **Shift+Enter to `ESC CR`** (what Option+Enter sends): xterm.js sends a plain
  `CR` for Shift+Enter, which submits the prompt in Claude Code (measured in WebKit); `ESC CR`
  inserts a newline. `macOptionIsMeta` is on.
- **Recorder.** Per holder: live chunks of at most 1 s or 11 KiB raw (base64 must fit the
  16 KiB relay frame with its envelope), carrying `fromOffset`, `cols`, `rows`, sent as
  `local.session.screen`. Every 30 s or 256 KiB it uploads an append-only **segment** of raw
  output plus a **keyframe** (the holder's snapshot) to S3 through a presigned PUT, then
  commits a manifest entry over the relay (§5.5). After an app restart it asks the holder to
  replay from its last committed offset; anything older is one `gap`. It flushes before the
  session's `Ended` is sent.
- **Remote start, pick-up and Resume** (`session_start.rs`, `session_pickup.rs` share one
  launcher, `session_pickup.rs:807` → `session_start::launch`) start a held session, bring the
  terminal window to the front and post a native notification. A hidden launch would lose the
  visibility today's Terminal.app launch gives the owner.
- **Start links** (`start_links.rs`, the `LIGHTBULB_SESSION_*` environment in
  `session_start.rs:310`) are deleted: the holder manager binds a start id to its holder
  directly.

### 5.3 The `lightbulb` command

`lightbulb-desktop cli` (a subcommand of the app binary). The app installs a symlink
`~/.local/bin/lightbulb` and shows the one-line PATH addition if that directory is not on
PATH. No admin prompt.

`lightbulb claude [args]` / `lightbulb codex [args]` asks the running app over the local
socket directory to start a held session in the current folder, prints
`● Shared with <workspace>. Run plain \`claude\` to keep a session private.`, then attaches
as the interactive client (raw mode, SIGWINCH → `resize`, terminal restored on every exit
path). If the app is not running it says so and exits 1; it never starts an unshared session.

### 5.4 What counts as shared

A session is shared iff its provider process descends from a holder:

- **Claude:** a `claude` process whose parent chain reaches a holder pid, and which is the
  first provider claimed in that holder (the first-claim rule in `start_links.rs:86-93`).
  The holder records the claimed `providerPid` + birth in `hello`. A `claude` started later
  inside that session's shell is not the first claim and stays private. The parent-chain walk
  already exists (`native_tools.rs:71-88`).
- **Codex:** bound by **thread id**, not pid. Codex 0.159.x writes rollouts through a shared
  app-server daemon (`codex.rs:48`), so the rollout writer is not the holder's child. The
  Codex `SessionStart` hook runs under the TUI; the hook walks its parent chain to a holder
  and records `{holderId → threadId}`. Codex mirrors are filtered by thread id.
- **Where the filter sits:** in discovery before a mirror is created
  (`transport.rs:849-858`: keep only held sessions before `tx.send`), and in the hook binary:
  a hook for a private session exits immediately, so private prompts get no `PermissionRequest`
  wait (`approval.rs:30`, 2 s) and no tool input reaches the app.
- **Server side:** `local.session.register` carries `holderId`. After this release the backend
  refuses registrations without it and ends existing unheld sessions itself, so older desktop
  builds cannot keep sharing plain sessions.
- The Claude `kind: "background"` path (`claude.rs:25,165`) is deleted. The user-scope MCP
  server and skill stay; a private session's `send_message` is refused by attestation
  (`native_tools.rs:131-137`).

Identity is unchanged: session id = uuid5(owner, device, provider, profile, conversation);
generation = the provider process's pid + birth.

### 5.5 Recording and the live view

- **Live:** `local.session.screen {sessionId, generation, fromOffset, cols, rows,
  kind: "output"|"gap", data}` on the existing relay. The backend does **not** store it and
  does **not** write the session META row (META's revision guards sends and approvals,
  `local_sessions_repo.py:246`; a 1 Hz writer there would starve them). It checks the row
  (generation, not ended, not `unshared`, enrollment) and publishes to current viewers off
  the ack path. While `unshared`, chunks are acked and dropped and the recorder writes a `gap`.
- **Viewers:** `POST /messaging/sessions/{id}/viewing` every 15 s writes a `VIEWER#<user>` row
  with a 45 s TTL in the session partition. Its only job is to bound fan-out to people
  actually watching. Publishing uses each viewer's existing user channel
  (`live_events.publish_user`), which already stops for members who have been removed.
  The new event type is added to `USER_LIVE_EVENT_TYPES` and the shared contract.
- **Stored recording:** segments and keyframes in S3 under
  `session-recording/<owner>/<sessionId>/` (outside the `<owner>/` prefix agent runtimes can
  read; same reasoning as `session_pickup.py:13-15`). The recorder uploads with a presigned
  PUT; `local.session.recording` commits `{segmentNo, fromOffset, toOffset, keyframe}` as a
  `SEGMENT#<generationNo>#<segmentNo>` row (the offset restarts with each holder, so the
  generation number keeps keys unique across Resume).
- **Viewer:** `GET /messaging/sessions/{id}/recording` returns the segment list with
  presigned GETs. A viewer opening a live session loads the latest keyframe and the live
  chunks after it; scrolling back fetches earlier segments. An ended session is the same
  without live chunks. While the Mac is offline the last screen stays with an "Offline" note.
  Sessions recorded before this release fall back to the session view.
- **Resume appends, never replaces.** A resumed session keeps its session id; its new run is
  recorded under the next generation number and shown after the earlier run, separated by a
  line "Resumed by <Name> · <time>". The earlier recording stays as it was.
- **Bounds:** the backend caps each session at 2 MiB per minute and 200 MiB in total; above
  that it records one `gap` and drops the rest.
- **Readers:** humans who can read the session. The `/viewing` and `/recording` routes refuse
  `AgentInvocation`, room-scoped and machine/MCP callers, and no agent or MCP tool exposes the
  recording.
- **Deletion:** session delete and the workspace / account wipes call
  `session_pickup.delete_prefix`-style deletion of the session's recording prefix alongside
  `local_sessions_repo.delete_owner`. Retention: open question 3.
- The messaging projector's DynamoDB stream subscription gets a key filter (`MSG#`, `LIVE#`)
  so session rows stop invoking it (`api-stack.ts:714-730`).

### 5.6 The structured record

Unchanged mechanism (hooks + transcript tail + `EVENT#` rows), restricted to held sessions. It
feeds the phone, agents (`lightbulb_local_sessions`), room @session, approval cards, delivery
receipts and offline send. #3614 is required.

### 5.7 Resume

Resume has its own rail, because an ended session has no live connection or command lease
(`dispatch_session` needs both):

- **Who:** the owner, and humans who were allowed to send when the session ended. `_ended`
  currently deletes `allowedSenders` (`local_session_relay.py:964-970`); it now copies them to
  `resumeAllowed` first. The owner can revoke `resumeAllowed` on the ended session. Agents
  cannot resume.
- **Request:** `POST /messaging/sessions/{id}/resume {message?}` writes one pending
  `RESUME#` row per session (conditional put, 24 h expiry) and a `RESUMEWANT#<device>#<sid>`
  row delivered to the Mac in the device-heartbeat acknowledgement, the way pick-up requests
  are (`session_pickup.ack_with_pickup`). That gives the offline wait without a new channel.
- **Guards:** `submissions_enabled` and the Mac's own consent switch, as for remote start; the
  same Mac, provider binary and folder; no new flags. Permission mode: the owner resuming gets
  the session's recorded mode capped at `default`; anyone else gets `plan`
  (`desktop_session_start.py:102-105` forces `plan` for the same reason).
- **Run:** the Mac refuses if a held session already owns that conversation (two
  `claude --resume` processes would fork it), otherwise starts `claude --resume <id>` /
  `codex resume <threadId>` in a new holder and shows the terminal window with a notification
  "<Name> resumed <title>" and a local **Stop it** button (the owner's own Mac, no remote
  rail).
- **First message:** when the successor registers, the backend seeds the resumer into its
  `allowedSenders` in the same transaction, then admits the first message as a normal send.
  Delivery is at most once: a send whose outcome is lost shows as "unknown", never sent twice.
- **Clients:** "Resume and send" on ended sessions on web, desktop and iPhone; a separate client
  predicate from today's "no control after end" (`sessionControls.ts:32`).

### 5.8 Phone

No terminal view. The session view, approval cards and message box as today, plus Resume.

## 6. Security and privacy

- **No remote keystrokes.** Only the local terminal window and `lightbulb claude` send input,
  and only the local window's own webview can call the holder commands (§5.2). Remote text
  goes through Claude's peer socket and `codex queue` with labels; prompts are answered only
  by the `PermissionRequest` hook for the exact tool call. A later remote Stop must be a fixed
  `Esc`, owner or team control, sent only when hooks say a turn runs and no prompt is open.
- **Resume by a teammate** starts a program on the owner's Mac. Bounded by: only people the
  owner allowed to send, the same folder and binary, `plan` mode, a visible window and
  notification, the owner's local Stop, and the Mac's consent switch.
- **Recording is unredacted.** It shows anything on screen. Accepted because sharing is
  explicit. Humans only; never agents (§5.5). Paused periods are not recorded.
- **Local sockets** are uid-checked.
- **Terms.** The unmodified Claude Code binary runs with the user's own login, which
  Anthropic's legal page does not restrict ("Nor does it prevent an end user from signing in
  to the unmodified Claude Code binary with their own Claude subscription").

## 7. Failure modes

| Event | Result |
|---|---|
| App restarts, crashes or updates | Holders keep running. The app finds them, the window reattaches from a snapshot, the recorder replays from its last committed offset or writes a `gap`. |
| Holder crashes | The session ends, as if its terminal crashed. |
| Outside terminal closed | The CLI detaches; the session keeps running and stays in the app. |
| Mac sleeps | Viewers see "Offline" and the last screen. |
| Relay down | Live chunks are skipped; the recorder catches up from the holder's log or records a `gap`. |
| Holder protocol mismatch | "Restart this session to update it". |
| Old desktop build | Its registrations are refused after the release; its sessions are not shared. |

## 8. Migration

On release: the backend refuses registrations without `holderId` and ends existing unheld
sessions (which starts their pick-up retention, `pickup_stopped_at`). The desktop app shows
once: "Sessions are now shared only when you start them in Lightbulb or with
`lightbulb claude`." No flag and no compatibility path (owner ruling, 2026-09-12).

## 9. Later, not in this spec

Remote Stop (fixed `Esc`), slash commands from remote surfaces, agents reading the current
screen as text (the holder's terminal model makes this cheap), a terminal view on the phone,
Windows and Linux.

## 10. Kill tests before building

1. **Update survival, for real:** a holder started by the shipped app (from inside the
   bundle) survives `download_and_install` → `app.restart()` (`lib.rs:1367-1382`), and the
   new app reattaches and redraws Claude Code correctly.
2. **macOS identity:** a held `claude` reads the Keychain login; its first `~/Desktop` access
   while the app is quit, and again after an update, prompts at most once and does not fail;
   `which node git gh`, nvm, `locale` and Unicode glyphs match a login terminal; `git commit -S`
   and `ssh -T git@github.com` work.
3. **The terminal window:** xterm.js in WebKit handles Shift+Enter, Option as Meta, Cmd
   shortcuts, multi-line paste after attaching to a session with more than 1 MiB of output,
   image drop, resize without duplicated scrollback; IPC throughput and memory over 8 hours.
4. **Probes and no window:** a held `claude` started with no client attached starts normally
   and renders correctly when a window attaches later.
5. **Codex:** in a holder it renders; the hook's parent chain reaches the holder; the thread is
   mirrored; `codex queue` delivers.
6. **Esc and prompts:** `Esc` during a turn interrupts cleanly; record what it does with a
   permission prompt open (input to the later remote Stop).
7. **Volume and replay:** one real working hour's output volume, and the time to show a
   long session from a keyframe.

## 11. Open questions

1. Does closing the terminal window detach (session keeps running) or end the session?
2. Recording retention: as long as the session row, or a fixed period?
3. Should a teammate's Resume require anything more when the owner's Mac is locked or the
   owner is away?
4. Phase 1 needs the real app identity (kill tests 1 and 2). Is a founder-only first build
   acceptable (the `DESKTOP_EXECUTION_OWNER_ALLOWLIST` pattern), given the no-flags ruling?

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
  newline. Fix: map Shift+Enter to `ESC CR` in the terminal window (§5.2).
- Option+B moved back one word (`macOptionIsMeta`).
- A three-line `term.paste` (the Cmd+V path) stayed in the prompt; status stayed idle.
- The `/` menu opened and filtered as typed.
- `Esc` during a running reply showed "Interrupted"; `/exit` ended the session and the holder.
- Not covered: OS clipboard Cmd+C/Cmd+V, image drop (needs the real app), 8-hour memory.

Reviews: desktop architecture, backend/security architecture, and an over-engineering pass
(2026-10-02); their blocking findings are resolved in this revision. Research: Happy Coder
source, Superset (PTYs held by a host service, headless emulator snapshots), VibeTunnel,
Anthropic's legal and Agent SDK pages.
