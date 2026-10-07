# The real terminal, everywhere

Status: draft for review, 2026-10-07. Priority 1 of the 2026-10-07 brief. Supersedes, where they
differ, `2026-10-02-managed-session-sharing.md` rev 5 decisions 4 ("nothing is recorded or stored;
a session that is not running shows the existing session view") and 8 ("the phone keeps the
session view … no terminal"), and the 2026-10-03 "no recording" ruling (see §9, D1). Facts cite
`main` at `dfce908f8`; the research behind them is in
`docs/superpowers/plans/2026-10-07-terminal-everywhere-research.md`.

## 1. Problem

A shared session shows its real terminal only when the Mac can host or attach to it; everything
else is a reconstructed transcript drawn to look like the terminal:

- `LocalSessionPanel.tsx:65-78`: `hasTerminal = !!session.terminal`; without it the status line
  reads "Transcript · <status>", and the body is `NativeSessionTranscript`
  (`LocalSessionPanel.tsx:192-200, 258-268`).
- `local_sessions.py:652`: `terminal` is forced to `None` for every ended or offline session, so
  those never have a terminal image.
- `SessionTerminalView.tsx:11-12, 116-117`: thirty seconds with no frame flips a live terminal back
  to the transcript automatically.
- The iPhone has no terminal at all (`apps/mobile/src/app/(app)/session/[sessionId].tsx`: an
  inverted FlatList of transcript rows).
- Viewers get the visible screen only: `SessionTerminalView.tsx:93` runs xterm with
  `scrollback: 0`; a snapshot is "the visible screen and its modes" (`holder/model.rs:89-97`); the
  holder's 5000 vt100 scrollback rows and its 1 MiB output log are never sent
  (`holder/run.rs:18, 264`; no client sends REPLAY, `cli.rs:478` excepted).
- Nothing is recorded: the holder's log is in memory only and the socket is removed at exit
  (`holder/run.rs:264, 448-449`).

What the owner wants (brief, 2026-10-07): every shared coding session displays its actual native
terminal experience, never a transcript, never a transcript styled as a terminal, never an
automatic transcript fallback. Sessions started inside and outside Lightbulb, foreground,
background/attached, desktop coding agents, resumed sessions, earlier history, reconnect, offline
and ended sessions. One viewer's scrolling, history, focus or resizing must not change another
person's view. Concurrent input and drafts designed explicitly. Keep the mod, owner-only
admission, no steering rights to approve people, no blanket typing pauses, no native macOS
confirmations per operation. "Something like herdr."

## 2. What herdr is, and what to take from it

herdr (herdr.dev, v0.9.3, Apache-2.0, YC F26) is a terminal multiplexer for coding agents: a
persistent server owns every pane's pty and process tree; clients only render; several clients can
attach to one session; closing a client never stops work; `pane_history` (experimental) persists
screen contents across restarts; agents are classified working / blocked / done / idle / unknown
from the foreground process and "screen manifests" (pattern-matching the bottom of the live
screen); a CLI that is also a socket API lets agents spawn panes and prompt each other
(`herdr agent start`, `herdr agent prompt --wait`, `herdr pane split`). No browser or phone client,
no sharing with other people.

Lightbulb already has the shape: the holder is herdr's server pane (one process per session,
`setsid`, survives app restarts, `holder/manager.rs:186-203`); `lightbulb claude|codex`, New
session, remote start and the `lightbulb_desktop` tool are `herdr agent start`; hooks give state.
What is missing, and what this spec adds, is herdr's one property we do not have: **every session
is server-owned by construction**, so every session has a real screen with history, for every
client. We cannot own a pty we did not create, so we cover the rest with real-screen mirrors
and an explicit "no terminal" state, never a transcript.

## 3. Decisions (proposed; D1–D6 need the owner, §9)

1. **The human session view is a terminal or an explicit state. Never a transcript.**
   `NativeSessionTranscript`, `SubagentPanel`, `TranscriptRow` and the phone's transcript list leave
   the human session view on every surface. The structured record stays where machines read it:
   agents (`lightbulb_local_sessions read`, SKILL.md:47), approval cards and `pendingApproval`,
   Details (plan / changed files from `sessionPlan` / `sessionChanges`), pick-up, the public link
   page `/s` (Continue), and the EVENT# rows that drive `latestEventSeq`, titles and push copy.
2. **Four hosting mechanisms, chosen per session, reported honestly.** The public session carries
   `terminal: "held" | "attach" | "mirror" | "window" | null` plus `terminalReason` when null.
   The viewer renders a terminal for the first four and an explicit state for null (§4).
3. **Hosting is the default.** "Share every session live" is on when sharing is turned on at
   pairing (today it defaults off, `DesktopPermissionsGate.tsx:53`, and is offered once later,
   `ExplicitSharingNotice.tsx:29-69`). The consent screen says what it writes
   (`~/.zshrc`, `~/.bashrc`, the bash login file; `shell_integration.rs:7-43`). The switch stays
   in Settings. (D2)
4. **Viewers own their own view.** Each viewer renders its own xterm at the owner's cols×rows,
   scaled to fit, with local scrollback; scrolling, selection, focus and the viewer's window size
   never reach the Mac (`SessionTerminalView.tsx:23, 76-87` already). The one terminal that sizes
   the pty is the owner's (their outside terminal, or their app panel when it is the interactive
   client, `holder/run.rs:383-389`, `terminal.js:38-44`), as in any multiplexer.
5. **History travels with the stream.** A new viewer gets the holder's recent output (up to
   `BURST` 256 KiB of the 1 MiB log, `screen.rs:43`) as `kind: "history"` frames before the
   snapshot, into an xterm with scrollback. Periodic snapshots no longer reset the viewer; a
   snapshot resets only after a gap (seq or epoch), so scrollback survives (§5.3).
6. **Ended and offline sessions replay their recording.** The holder writes an asciicast v2 file
   (output and resize events with timestamps) and the Mac uploads it to a backend-only S3 prefix;
   web and phone replay it in xterm, with Resume beside it. A session with no recording (ended
   before this shipped, or recording refused) shows the explicit "No recording" state, never the
   transcript. This reverses the 2026-10-03 ruling (D1).
7. **Concurrent input is explicit** (§6): admitted typists choose Keys (raw, as today) or Line
   (a private draft sent as one atomic frame). The owner's terminal is never paused; the mod's
   band keeps the 2 s "a teammate just typed" guard and the typed-prompt labels (#3772).
8. **The phone gets the same terminal** in a WebView running the same xterm page as the web, fed
   the same frames, with a key bar and Line mode as the default input (D4).
9. **Nothing here changes who may type or admit.** Watching = anyone who can read the session;
   typing = owner, allowed senders, team control (`may_control`, `local_sessions.py:769-772`);
   admitting = owner only (band, app, phone). Steering rights never include Allow/Decline.
10. **Agents never get the screen or the keyboard.** Unchanged (rev 5 §5.5, §6).

## 4. Coverage: every session origin and what it shows

| # | Origin | Today (`terminal`) | After this spec | Mechanism |
|---|---|---|---|---|
| a | Started in Lightbulb (New session, remote start, pick-up, Resume, `lightbulb claude|codex`) | held | held | holder (unchanged) |
| b | Plain `claude` from an interactive zsh/bash with the switch on | held | held | shell functions → `lightbulb claude` (`shell_integration.rs:32-43`); **default on** (D2) |
| c | `claude --bg` job | attach | attach, with history | `claude attach <job>` in an attach holder (`holder/manager.rs:69, 86-90`) |
| d | Plain `claude` moved with ← | attach (fork) | attach; the origin's recording is chained to the fork (§5.4) | as c |
| e | Plain `codex` on the shared daemon | attach | attach | `codex resume <thread> --remote unix://` (`holder/manager.rs:74`) |
| f | Plain `claude` in a terminal, switch off or shell already open (tmux, iTerm2, Terminal.app, kitty, WezTerm) | none → transcript | **mirror** (read-only real screen, polled) | host-terminal reader (§5.5); plus the "Move to Lightbulb" action (← for Claude) |
| g | Plain `codex --no-daemon` | none → transcript | mirror | as f |
| h | Claude Code in VS Code / Cursor / JetBrains panel, Codex desktop app (ChatGPT.app), Claude Desktop Code tab | none / not tracked → transcript | **window** (pixels, read-only) after the spike; until then the explicit state "No terminal · runs inside VS Code" | ScreenCaptureKit window of the owning app (§5.6, D3) |
| i | `claude -p`, SDK runs, routines | not tracked | not tracked (no TUI exists) | none; listed nowhere |
| j | Any of a–h, ended | transcript | **recording** replay + Resume | §5.4 (D1) |
| k | Any of a–h, owner's Mac offline or app quit | transcript | recording replay up to the last upload + "Mac offline" | §5.4 |
| l | Any of a–h, resumed | transcript until the new process draws | recording of the previous generation, then the live terminal | §5.4 chains generations |
| m | Viewer reconnects / tab was hidden | snapshot ≤10 s later, no history | history + snapshot at once (`local.session.watch` nudge, `session_screen.py:111-123`) | §5.3 |
| n | iPhone, any of the above | transcript | the same terminal in a WebView; Line mode input | §7 (D4) |

"Explicit state" means a labelled panel with the session's status line, the one action that gets
a terminal (turn on the switch, press ←, open in Lightbulb, Resume), and nothing drawn from
events. It is never styled as a terminal.

## 5. Design

### 5.1 Session row and public contract
- Mac heartbeat `terminal`: `held | attach | mirror | window | none`, plus `terminalReason` for
  none (`shell-off`, `ide`, `app`, `sdk`, `unknown`). Backend `session_terminal`
  (`local_sessions.py:588-594`) passes `mirror`/`window` through; `_session_public` keeps
  `terminal` for ended/offline sessions **only as `recording: {available, through, parts}`**, so
  clients never treat an ended session as live.
- `LocalSession` (`packages/shared/src/messaging/types.ts:366-441`) gains `terminalReason?`,
  `recording?`, `inputMode?`.

### 5.2 Relay frames (Mac → backend → viewers)
Unchanged transport: `local.session.screen` over the relay, `session_screen.forward` to each
viewer's user channel, 2 MiB/min per session (`session_screen.py:150-151, 197-238`). New
`kind` values on the same frame: `history` (replayed log bytes, sent before the first snapshot
for a new viewer set, ≤ `BURST`), `ping` (no data; proves the stream is alive so a quiet screen
is not "stale"), and for `window` sessions `kind: "image"` (JPEG, as `agent_cast.rs` adapts:
quality 35–80, width 1280→800, ≤200 KB base64, ≤10 fps, `agent_cast.rs:20-30`). The backend
validates `image` frames against a separate per-session budget (1 MiB/min default) and refuses
them for sessions whose `terminal` is not `window`.

### 5.3 Viewer (web, desktop, phone)
- xterm `scrollback: 5000` (the desktop panel's value, `terminal.js:5`).
- Apply `history` frames first, then the snapshot: the first snapshot resets; a later snapshot
  with no gap is ignored (it carries nothing new); a snapshot after a gap (held frames overflow,
  `sessionScreen.ts:21-22`, or a new epoch) clears the visible screen (`ESC[2J ESC[H`) and
  redraws, so scrollback survives at the cost of one duplicated screen on a lost frame.
- "Stale" means no frame and no `ping` for 30 s: the viewer greys the picture and says
  "Reconnecting…"; it never swaps in a transcript (removes the fallback at
  `SessionTerminalView.tsx:11-12, 116-117`).
- The viewer never resizes the Mac. Its scale follows its own box (`fit`, `:76-80`).
- `sessionScreen.ts` ordering moves to `packages/shared` for the phone.

### 5.4 Recording and replay (D1)
- **Holder:** at the output append (`holder/run.rs:294-306`, line 302) also append
  `[t, "o", text]`; at resizes (`:332-338`, `:383-389`) append `[t, "r", "COLSxROWS"]`; write the
  asciicast v2 header at start. File `~/Library/Application Support/<bundle>/recordings/<holder>.cast`,
  mode 0600, capped at 64 MiB (the oldest 8 MiB is dropped with an `"m"` marker when the cap is
  hit; `LOG_CAP` stays for live replay). Attach holders record the same way (they are clients of
  the job, and get the job's own output). Mirror sessions record their polled snapshots as
  `"o"` events after a clear. Window sessions record nothing (pixels; D3).
- **Upload:** the Mac uploads parts (gzip, ≤16 MiB) with presigned PUTs over the pick-up rail
  (`routers/desktop_session_pickup.py`, `presign_uploads`, `session_pickup.py:666-691`) to a new
  backend-only prefix `session-recordings/<owner>/<sid>/<generation>/t-NNNNNN.cast.gz` with its
  own IAM statement beside `SessionPickupRW` (`service-stack.ts:960-982`); never under
  `<owner>/*`, which the workspace's agent runtime can read. Cadence: at every `Stop` hook and at
  least every 5 minutes while the session is shared; always at session end. A `RECORDWANT` is
  unnecessary: recording is on for every shared session; pausing sharing (`unshared`) stops
  upload and deletes what was uploaded for that session.
- **Chaining:** a resumed session (new generation, same sessionId) replays the previous
  generation's parts first, then its own; a fork (`forkedFrom`) replays its origin's parts first.
- **Playback:** `GET /messaging/sessions/{id}/recording` (human session token, `authorize_session`
  read, `_still_a_member`) returns presigned GETs for the parts in order; the viewer streams them
  into xterm (same component, no live input), with a "Resume" control for ended sessions and
  "Mac offline" for lapsed leases. Public links keep today's behaviour (no recording to outsiders)
  until the owner says otherwise.
- **Retention:** 30 days after the session ends, then the sweeper deletes (pick-up's
  `sweep_once` pattern, `session_pickup.py:854-885`). Deleting a session, unsharing it, or the
  owner leaving the workspace deletes its recording. (D5)

### 5.5 Host-terminal mirror (origins f, g)
A read-only real screen for a session whose pty belongs to another terminal. The app already
knows the provider pid, its parent, its tty (`ps -o tty=`, `screen.rs:192-212`), its open files
(`lsof`, `local_sessions.rs:1075-1086`) and command line (`claude.rs:155`). From the tty:
- **tmux**: `tmux list-panes -a -F '#{pane_tty} #{pane_id}'` → `capture-pane -e -p -S -5000`
  (ANSI, with scrollback). tmux is installed on the founder's Mac and is how acceptance sessions
  run today.
- **iTerm2**: AppleScript `tty of session` → `contents`; **Terminal.app**: `tty of tab` →
  `contents` (plain text); **kitty**: `kitty @ get-text --ansi --extent all`; **WezTerm**:
  `wezterm cli get-text --escapes`.
Polled every 500 ms while watched, diffed, sent as `kind: "snapshot"` frames; the mirror has no
`type` client (read-only), and the viewer says "Watching · <tmux> on <Mac>". The owner gets one
action: "Open in Lightbulb" (Claude: press ← in that terminal; the panel then switches to the
fork). Automation consent (AppleEvents) is per target app and happens once, at the first mirror,
inside the app's own Settings → Desktop explanation; it is not a per-operation prompt.

### 5.6 Window mirror (origin h; spike first, D3)
For sessions whose host is an app with no screen API (VS Code, Cursor, JetBrains, ChatGPT.app,
Claude Desktop): capture that window with ScreenCaptureKit (the app already holds the Screen
Recording permission plumbing, `src/screen.rs:27-98`), matched by the owning pid from the
parent walk, and stream `image` frames on the agent-cast adaptation. Read-only; no recording;
no typing. Ships only after the spike in the plan passes (CPU, permission UX, window tracking).
Until then origin h shows the explicit state.

### 5.7 Desktop panel (owner's Mac)
Unchanged host (`holder/panel.rs`): the child webview attaches as `interactive`+`join`. It now
asks for `replayFrom` so its 5000-line scrollback is full on open (today a snapshot only,
`panel.rs:148-158`). End stays two clicks. The mod band is unchanged.

## 6. Concurrent input and drafts

Admitted typists (owner, allowed senders, team control) have two modes, shown as a toggle under
the terminal and remembered per person:

| Mode | What it sends | Draft visibility | Use |
|---|---|---|---|
| **Keys** (today) | every keystroke as typed, ≤4096 B frames (`session_screen.py:244-250`) | shared: the provider's prompt line is the draft, everyone sees it | answering y/n, Esc, arrows, slash menus, shortcuts |
| **Line** (new; default for teammates and on the phone) | the whole line as ONE frame: bracketed paste (`ESC[200~…ESC[201~` when the holder reports mode 2004 on) + CR | private until sent; the terminal shows it only when it lands | prompts and follow-ups |

Rules that make two people safe on one prompt:
- The holder keeps `lastInput` (`holder/run.rs:377-382, 458-460`); the mod labels a submitted
  prompt by who pressed Enter (#3772) and refuses band decisions for 2 s after a teammate's bytes
  (`remote_input.rs:39, 120-122`). Unchanged.
- A Line send is appended to whatever is in the provider's prompt. The typist can see that line
  on screen; the composer shows "The prompt line is not empty" when the last screen row has text
  after the prompt glyph (a view-side heuristic, no new wire), and still lets them send.
- No typing pause for anyone, ever (owner ruling). The band's "Aden typing" stays the signal.
- Agents cannot type (unchanged). Admission never changes here.

## 7. Phone

- A WebView (`react-native-webview 13.16.1`, already in the binary, so OTA-safe) hosting the same
  xterm page the web uses, loaded from the product origin (`/terminal`, a static page with the
  vendored xterm bundle and no network of its own). React Native feeds it frames with
  `injectJavaScript` from the live events it already receives on the user channel
  (`providers/live-events.tsx:120-130`); input goes back by `postMessage` to
  `sendLocalSessionInput` (`client.ts:101, 181`). Pinch zoom scales the page; the viewer never
  resizes the Mac.
- Key bar: Esc, Tab, Ctrl, ↑ ↓ ← →, Enter, plus the Line composer (default).
- Backgrounded app: the socket is off (`live-events.tsx` `enabled: foreground`); on return the
  view shows "Reconnecting…" until history + snapshot arrive (≤10 s).
- Approval cards, Ask to join, Resume and Details stay as they are; the transcript list goes.
- The `expo-dom` alternative needs a new native module and a TestFlight build; the WebView does
  not. (D4)

## 8. Security and privacy

- Recording is a new store of unredacted terminal output (secrets included), readable by anyone
  who can read the session, kept 30 days after end, deleted on unshare / delete / member removal.
  It lives outside `<owner>/*` so no agent runtime can read it. Public links do not serve it.
- Mirrors read another program's screen on the owner's own Mac, only while the session is
  shared and someone watches; nothing is typed into a mirrored terminal.
- Window mirrors need Screen Recording; the permission is asked once in Settings → Desktop,
  never per session (consent-once-at-pairing ruling).
- Every typing door keeps `get_human_session` (first-party Clerk session only, `auth.py:255-276`),
  `may_control`, `_still_a_member`, the 4096-byte frames and rate limits, the TYPED# audit rows,
  and the owner-only band keys (`fromOwner`, `remote_input.rs:330-350`).
- Line mode sends exactly what the person typed plus CR; no text is synthesised.

## 9. Owner decisions needed

- **D1 Recording.** Reverses 2026-10-03 "nothing is recorded". Needed for ended, offline and
  resumed sessions to show a real terminal; without it those states are explicit "No recording"
  panels. Default proposed: on for every shared session, 30 days, deleted on unshare.
- **D2 Default-on hosting.** The shell switch on at pairing, written into the shell rc files
  with the consent screen saying so.
- **D3 Window mirror.** Pixels of IDE/app windows, one Screen Recording permission. Spike first.
- **D4 Phone terminal hosting.** WebView page from the product origin (OTA-safe) vs `expo-dom`
  (new native build). Proposed: WebView.
- **D5 Retention** of recordings (30 days proposed) and whether public "Anyone" links may ever
  replay them (proposed: no).
- **D6 Line mode as the teammate default** (Keys stays one tap away).

## 10. Not in this work

Windows and Linux hosts; a shared-cursor or multi-caret prompt; agent status from screen
manifests (hooks already give it); Codex desktop-app attach (`codex resume --remote` against
the app's own app-server socket is unverified); typing into mirrored terminals.

## 11. Evidence

Observed in code on `main` `dfce908f8` (citations above). Locally observed on the founder's Mac
2026-10-07: Claude Code 2.1.292 (`claude attach` opens background sessions only), Codex 0.159.3,
tmux installed, VS Code + Cursor + ChatGPT.app + Claude.app installed, every running Claude session
`interactive` on a tty (no attach path), it2/kitty/wezterm absent. Nothing in this spec has been
built or tested; the plan names the spikes and the acceptance runs.
