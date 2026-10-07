# The real terminal, everywhere

Status: revision 3, 2026-10-07, agreed by the owner (no pictures, no recordings, no mirror, no
Line mode; keys as in native Claude Code; typist-coloured cursor). No decisions remain (§9). Priority 1 of the 2026-10-07 brief. Supersedes,
where they differ, `2026-10-02-managed-session-sharing.md` rev 5 decision 8 ("the phone keeps
the session view … no terminal") and its "a session that is not running shows the existing
session view". Keeps rev 5 decision 4: nothing is recorded or stored. Facts cite `main` at
`dfce908f8`; the research is in `docs/superpowers/plans/2026-10-07-terminal-everywhere-research.md`.

## 1. Problem

A shared session shows its real terminal only when the Mac can host or attach to it; everything
else is a reconstructed transcript drawn to look like a terminal:

- `LocalSessionPanel.tsx:65-78`: `hasTerminal = !!session.terminal`; without it the status line
  reads "Transcript · <status>" and the body is `NativeSessionTranscript`
  (`LocalSessionPanel.tsx:192-200, 258-268`).
- `local_sessions.py:652`: `terminal` is forced to `None` for every ended or offline session.
- `SessionTerminalView.tsx:11-12, 116-117`: thirty seconds with no frame swaps a live terminal
  back to the transcript automatically.
- The iPhone has no terminal at all (`apps/mobile/src/app/(app)/session/[sessionId].tsx` is an
  inverted FlatList of transcript rows).
- Viewers get the visible screen only: `SessionTerminalView.tsx:93` runs xterm with
  `scrollback: 0`; a snapshot is "the visible screen and its modes" (`holder/model.rs:89-97`);
  the holder's 5000 vt100 scrollback rows and 1 MiB output log are never sent
  (`holder/run.rs:18, 264`; REPLAY is used only by the CLI, `cli.rs:478`).
- When the provider exits, the holder drains for one second, removes its socket and exits
  (`holder/run.rs:26, 429-451`), so the final screen is gone at once.

The owner wants (brief 2026-10-07): the actual native terminal for every shared session, never a
transcript, never a transcript styled as a terminal, never an automatic fallback; sessions
started inside and outside Lightbulb, foreground, background, desktop coding agents, resumed,
earlier history, reconnect, offline and ended; viewers never change each other's view;
concurrent input and drafts explicit; the mod and owner-only admission unchanged; no typing
pauses; no per-operation macOS confirmations. "Something like herdr." No pictures of windows and
no recordings (2026-10-07).

## 2. What herdr is, and what to take from it

herdr (herdr.dev, v0.9.3, Apache-2.0) is a terminal multiplexer for coding agents: a persistent
server owns every pane's pty; clients only render; several clients attach to one session;
closing a client never stops work; a pane whose program exited keeps its screen until the server
restarts; agents are classified from the foreground process and the live screen; a CLI that is
also a socket API lets agents start and prompt each other. No browser or phone client, no sharing.

Lightbulb already has the shape: the holder is herdr's server pane (one process per session,
`setsid`, survives app restarts, `holder/manager.rs:186-203`); `lightbulb claude|codex`, New
session, remote start and the `lightbulb_desktop` tool are `herdr agent start`; hooks give the
state. What this spec adds is herdr's one missing property: every session is server-owned by
construction, so every session has a real screen with history for every client, and a screen
that stays after its program exits.

## 3. Decisions

1. **The human session view is a terminal or an explicit state. Never a transcript.**
   `NativeSessionTranscript`, `SubagentPanel`, `TranscriptRow` and the phone's transcript list
   leave the human session view on every surface. The structured record stays where machines
   read it: agents (`lightbulb_local_sessions read`, SKILL.md:47), approval cards and
   `pendingApproval`, Details (plan and changed files), pick-up, the public link page `/s`, and
   the EVENT# rows behind `latestEventSeq`, titles and push copy.
2. **Only a terminal we own or can attach to is shown.** No pictures of windows, no stored
   recordings, no polled mirrors of other terminal apps (owner, 2026-10-07). Two live sources: a
   holder (`held`) or a second real client of the provider's own host (`attach`). Where neither
   exists the view is an explicit state with one action (§4).
3. **Hosting is the default.** "Share every session live" is on when sharing is enabled at
   pairing (today off by default, `DesktopPermissionsGate.tsx:53`, offered once later,
   `ExplicitSharingNotice.tsx:29-69`). The consent screen says what it writes (`~/.zshrc`,
   `~/.bashrc`, the bash login file, `shell_integration.rs:7-43`). The switch stays in Settings.
   Agreed by the owner 2026-10-07.
4. **An ended session keeps its screen.** The holder no longer exits with its provider: it keeps
   the final screen and scrollback and serves them to viewers until the app restarts, like a
   terminal tab whose program exited. Nothing is written to disk. Resume starts a new holder;
   the ended one closes when the new one registers.
5. **Viewers own their own view.** Each viewer renders its own xterm at the owner's cols×rows,
   scaled to fit, with local scrollback. Scrolling, selection, focus and the viewer's window size
   never reach the Mac (`SessionTerminalView.tsx:23, 76-87` already). The one terminal that sizes
   the pty is the owner's own, as in any multiplexer.
6. **History travels with the stream.** A new viewer gets the holder's recent output (up to
   `BURST` 256 KiB of the 1 MiB log, `screen.rs:43`) as `history` frames before the snapshot.
   Snapshots no longer reset the viewer except after a gap, so scrollback survives (§5.3).
7. **Keys as in native Claude Code, and a cursor that says who types** (§6). Every admitted
   person's keys go straight into the terminal as typed. Nobody is ever paused. In the Lightbulb
   views the terminal cursor takes the colour of the last typist with their name beside it for a
   few seconds. The mod's band keeps its 2 s guard and typed labels (#3772).
8. **The phone shows the same terminal** in a WebView running the same xterm page as the web.
9. **Who may watch, type and admit does not change.** Watching = anyone who can read the
   session; typing = owner, allowed senders, team control (`may_control`,
   `local_sessions.py:769-772`); admitting = owner only. Agents never get the screen or keys.

## 4. Coverage: every session origin and what it shows

| # | Origin | Today | After this spec | Mechanism |
|---|---|---|---|---|
| a | Started in Lightbulb (New session, remote start, pick-up, Resume, `lightbulb claude|codex`) | held | held | holder, unchanged |
| b | Plain `claude` or `codex` from a new interactive zsh/bash | held only if the switch is on | held | shell functions → `lightbulb <tool>`; on by default |
| c | `claude --bg` job | attach | attach, with history | `claude attach <job>` in an attach holder (`holder/manager.rs:69, 86-90`) |
| d | Plain `claude` moved with ← | attach (fork); origin hidden | attach | as c; the fork's history is the fork's own |
| e | Plain `codex` on the shared daemon | attach | attach | `codex resume <thread> --remote unix://` (`holder/manager.rs:74`) |
| f | Plain `claude` in a shell opened before the switch, or with the switch off | none → transcript | explicit state "Press ← in its terminal" → attach | ← moves it to a background job (case c) |
| g | Plain `codex --no-daemon` outside Lightbulb | none → transcript | explicit state "Start it again with `lightbulb codex`" | no attach path exists off the daemon |
| h | Claude Code inside VS Code, Cursor or JetBrains; Codex desktop app; Claude Desktop Code tab | none / not tracked → transcript | **explicit state** "Runs in <app>, not in a terminal" + owner action **Open in a terminal** | no terminal exists; the action resumes the conversation in a holder (§5.6) |
| i | `claude -p`, SDK runs, routines | not tracked | not tracked | no TUI exists |
| j | Any held session, ended | transcript | the **final screen**, live from the Mac, + Resume | holder keeps its screen (§5.4) |
| k | Any attach session, ended | transcript | explicit state "Ended" + Resume | the provider's host closed its own screen |
| l | Owner's Mac offline or app quit | transcript | explicit state "Mac offline" + Resume (when back) + Pick up | nothing to stream |
| m | Resumed session | transcript until the new process draws | the real terminal; the tool redraws the conversation itself | holder |
| n | Viewer reconnects or returns from a hidden tab | snapshot ≤10 s later, no history | history + snapshot at once | §5.3 |
| o | iPhone, any of the above | transcript | the same terminal or state | §7 |

An explicit state is a labelled panel with the status line, the reason, and one action. It draws
nothing from events and is never styled as a terminal.

## 5. Design

### 5.1 Session row and public contract
- Mac heartbeat `terminal`: `held | attach | none`, plus `terminalReason` for none
  (`plain`, `no-daemon`, `app`, `sdk`, `unknown`). `_session_public` keeps `terminal` on an **ended held** session while its
  holder still serves the screen (`screenAvailable: true`), so the client can watch it; offline
  sessions keep `terminal: null`.
- `LocalSession` (`packages/shared/src/messaging/types.ts:366-441`) gains `terminalReason?`,
  `screenAvailable?`, `inputMode?`.

### 5.2 Relay frames (Mac → backend → viewers)
Unchanged transport: `local.session.screen` over the relay, `session_screen.forward` to each
viewer's user channel, 2 MiB/min per session (`session_screen.py:150-151, 197-238`). New `kind`
values: `history` (replayed log bytes, sent before the first snapshot for a new viewer set,
≤ `BURST`) and `ping` (no data; proves the stream is alive while the screen is quiet). `watch`
(`session_screen.py:63-108`) admits an ended held session while `screenAvailable` is true.

### 5.3 Viewer (web, desktop, phone)
- xterm `scrollback: 5000` (the desktop panel's value, `terminal.js:5`).
- Apply `history` first, then the snapshot: the first snapshot resets; a later snapshot with no
  gap is ignored; a snapshot after a gap (held frames overflow, `sessionScreen.ts:21-22`, or a
  new epoch) clears the visible screen and redraws, so scrollback survives at the cost of one
  duplicated screen on a lost frame.
- Stale = no frame and no `ping` for 30 s: the picture greys and reads "Reconnecting…". The
  transcript fallback at `SessionTerminalView.tsx:11-12, 116-117` is removed.
- The viewer never resizes the Mac. `sessionScreen.ts` ordering moves to `packages/shared`.

### 5.4 The ended screen (held sessions)
- Holder: after the provider exits and `EXIT_DRAIN` passes (`holder/run.rs:281-287`), the holder
  sends EXIT to clients (as today) but keeps its socket, model and log, and goes on answering
  `watch` HELLOs with the final snapshot and history. It accepts no input. It exits when the app
  tells it to (a Resume registered, the owner pressed End on the ended panel, the session was
  unshared) or when the app restarts and finds no tracked session for it. Nothing is written to disk.
- App: `terminal()` reports `held` with `screenAvailable` for such a holder; discovery keeps the
  ended session's record alive (today `transport.rs:756-767` ends it on the SessionEnd hook or a gone pid:
  the end still happens, only the screen stays).
- Viewer: the final screen with a "Ended · <time>" status line and Resume. Resume starts a new
  holder (`resume.rs:88-113`, unchanged); when it registers, the panel switches to the live
  terminal and the ended holder closes.

### 5.5 Sessions that have no terminal (origin h)
VS Code, Cursor and JetBrains run Claude Code in a chat panel; the Codex desktop app and Claude
Desktop are GUIs. There is no terminal to show and nothing is captured. The panel reads "Runs
in <app>, not in a terminal" with the owner-only action **Open in a terminal**: the app starts
`claude --resume <conversation>` (or `codex resume <thread>`) in a holder through `start_held`
(`session_start.rs:346-354`), the same path Resume uses, and the real terminal appears for
everyone. The original app session keeps running; the two share one conversation, so the owner
should continue in one place (the panel says so). Owner-only because it starts a process on
their Mac.

### 5.6 Desktop panel (owner's Mac)
Unchanged host (`holder/panel.rs`): the child webview attaches as `interactive`+`join`. It now
asks for `replayFrom` so its 5000-line scrollback is full on open (today a snapshot only,
`panel.rs:148-158`). End stays two clicks. The mod band is unchanged.

## 6. Concurrent input, cursors and drafts

Admitted typists (owner, allowed senders, team control) type as in native Claude Code: every key
goes to the Mac as typed, in ≤4096-byte frames (`session_screen.py:244-250`). There is no
separate draft box and no typing pause.

- **Whose keys are landing.** The relay already names a teammate's frame (`actor`,
  `session_screen.py:363-368`) and the holder keeps `lastInput` (`holder/run.rs:377-382, 458-460`).
  The Mac adds `typist: {userId}` to the next screen frames after a teammate's input, for 4 s.
  Every Lightbulb view (web, the Mac app's panel, the phone) draws the terminal cursor in that
  person's colour (the mod's `colorFor` palette, `rules.ts`) with a small name tag beside it, and
  returns to the plain cursor when the owner types or the 4 s pass. The owner's own iTerm cannot
  be recoloured; there the band's "Aden typing" and the coloured prompt label (#3772) stand.
- **Presence.** Viewers see who is watching and who may type (the band's list, already on the
  Mac; shown in the Lightbulb views too).
- **Drafts.** The provider's prompt line is the one draft, visible to everyone, as in a shared
  terminal. The mod labels a submitted prompt by who pressed Enter (#3772) and refuses band
  decisions for 2 s after a teammate's bytes (`remote_input.rs:39, 120-122`). Unchanged.

## 7. Phone

A WebView (`react-native-webview 13.16.1`, already in the binary, OTA-safe) hosting the same
xterm page the web uses, served from the product origin (`/terminal`, static, no network of its
own). React Native feeds it frames with `injectJavaScript` from the live events it already
receives on the user channel (`providers/live-events.tsx:120-130`); input returns by
`postMessage` to `sendLocalSessionInput` (`client.ts:101, 181`). Pinch zoom scales the page; the
viewer never resizes the Mac. Key bar: Esc, Tab, Ctrl, arrows, Enter, and the keyboard types
straight into the terminal. Backgrounded app: the socket is off; on return the view reads "Reconnecting…" until
history and a snapshot arrive. Approval cards, Ask to join, Resume and Details stay; the
transcript list goes.

## 8. Security and privacy

- Nothing new is stored: no recording, no capture. An ended screen lives in the holder's memory
  on the owner's Mac and is served only to people who can read the session, only while the app
  runs.
- Every typing door keeps `get_human_session` (first-party Clerk session only, `auth.py:255-276`),
  `may_control`, `_still_a_member`, the 4096-byte frames and rate limits, the TYPED# audit rows,
  and the owner-only band keys (`fromOwner`, `remote_input.rs:330-350`).
- "Open in a terminal" and Resume start a process on the owner's Mac: owner-only (Open) and the
  existing Resume rules (owner and allowed senders).

## 9. Decisions

Agreed by the owner 2026-10-07: terminal or explicit state, never a transcript; no pictures; no
recordings; no mirrors of other terminal apps; the shell switch on by default at pairing; keys as
in native Claude Code, no Line mode; a typist-coloured cursor in the Lightbulb views; the ended
screen lives only while the app runs. Decided here: the phone host is a WebView; "Open in a
terminal" is owner-only. Nothing is open.

## 10. Not in this work

Windows and Linux hosts; mirrors of other terminal apps (declined); agent status from screen
manifests; Codex desktop-app attach (`codex resume --remote` against the app's own socket is
unverified); window capture and recordings (declined); Line mode (declined).

## 11. Evidence

Observed in code on `main` `dfce908f8` (citations above). Locally observed on the founder's Mac
2026-10-07: Claude Code 2.1.292 (`claude attach` opens background sessions only), Codex 0.159.3,
VS Code, Cursor, ChatGPT.app and Claude.app installed, every running Claude
session `interactive` on a tty, it2/kitty/wezterm absent. Nothing in this spec has been built or
tested; the plan names the acceptance runs.
