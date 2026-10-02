# Managed session sharing

Status: draft for review, 2026-10-02. Owner: founder. Mockups: "Session Sharing Screens",
https://claude.ai/artifact/Jf4r2sd92ubE3pcgG9BnHZ (private to the owner).

## 1. Problem

Shared Claude Code / Codex sessions today are found by discovery (every CLI process on a
paired Mac), mirrored from their transcript files, and redrawn on web and phone as an
imitation of each terminal. Three things are wrong with that:

- **It is not where you code.** The desktop app is a delayed viewer. The owner wants to
  "feel comfortable using the desktop app for my coding".
- **It does not look like the real thing.** The transcript has no partial text, no spinner,
  no status line, and each Claude Code / Codex release changes the look we imitate.
- **It falls behind.** On 2026-10-02 a busy session's panel was ~5 hours stale
  (`Pondering… (19179s)`): one upload batch per 500 ms across every mirrored session, and
  each refill read ≤32 rows from every open subagent before the main thread. Draft PR #3614
  fixes the pacing and ordering.

## 2. Decisions (owner, 2026-10-02)

1. **Sharing is explicit, by how a session starts.** Shared iff started from inside the
   Lightbulb desktop app or with the `lightbulb` command (`lightbulb claude`,
   `lightbulb codex`). Plain `claude` / `codex` is private and Lightbulb ignores it.
   No per-session switch, no setting, no shim.
2. **No SDK, no headless mode.** The real, unmodified Claude Code / Codex terminal program
   runs in a pseudo-terminal (pty) that Lightbulb holds. Not Happy's kill-and-relaunch.
3. **The owner codes in a terminal panel inside the desktop app.** The same session can be
   attached from an outside terminal with `lightbulb claude`.
4. **Teammates see the same real view**, live while the session runs and recorded
   afterwards. The terminal output is recorded and stored, unredacted (accepted).
5. **Resume**: the owner and teammates allowed to send can resume an ended session; it must
   work from the iPhone.
6. **The phone keeps the session view** (the structured record), plus Resume. No terminal
   view on the phone.
7. **Teammates and agents contribute through today's labelled path**, never by typing into
   the terminal. (Review finding; not contested.)

Sessions that cannot be started this way are not shareable: the Codex desktop app
(`ChatGPT.app`), IDE extensions, and Claude Code background sessions. Codex desktop
sessions are shareable today and stop being so.

## 3. What this supersedes

The spec replaces these recorded decisions; the parity plan must be edited to point here:

- `docs/superpowers/plans/2026-10-01-native-session-parity.md` line 7: "Discovery remains the
  default … Managed launch is paused at the owner's request."
- Same file, B4 and line 115: "no opt-in controlled mode; companion mode … is the product."
  Lightbulb now hosts the native client. It still does not drive it headless.
- Same file, line 113, "workspace reading is shared by default": reading stays
  workspace-wide, but only for sessions started through Lightbulb.
- `docs/superpowers/specs/2026-10-02-one-lightbulb-mcp.md` surfaces table, "Mac with the
  Lightbulb app, sharing enabled": becomes "a session started through Lightbulb".
- The remote-start rule "only the member who paired that Mac"
  (`core/services/desktop_session_start.py:3`) gains one exception: Resume by a teammate
  allowed to send (decision 5).

Kept unchanged: B5 "Never … turn teammate text into owner input" (decision 7), the
`PermissionRequest` hook as the only remote way to answer a prompt, owner-only sending
unless the owner admits someone (Ask to join, team control), desktop consent given once at
pairing.

## 4. What people see

The artifact shows twelve moments. In short:

| Who | In the Lightbulb app | Outside the app |
|---|---|---|
| Owner, started in the app | The real terminal in a panel | — |
| Owner, started with `lightbulb claude` | The session is listed; opening it attaches the panel to the same terminal | The real terminal, unchanged, after one "Shared with <workspace>" line |
| Owner, plain `claude` | Nothing | The real terminal, private |
| Teammate (web, desktop) | The real terminal view: live, frozen with an "Offline" note while the Mac is away, recorded after it ends; message box and Resume under it | — |
| Owner or teammate on the phone | The session view, approval cards, message box, Resume | — |
| Agents (Friday) | The structured record, as today | — |

## 5. Design

```
 owner's Mac                                                     backend / clients
 ┌──────────────────────────────────────────────┐
 │ holder (one per session, detached, lives       │
 │ outside the app bundle)                        │
 │   pty ── claude / codex (unmodified)           │
 │   unix socket: attach · input · resize · output│
 └───────▲───────────────────▲──────────────────┘
         │                   │
  `lightbulb claude`   Lightbulb desktop app
  (outside terminal,    ├─ terminal panel (xterm.js)     ──► your screen
   thin attach client)  ├─ recorder: output chunks ──────► local-session relay ──► session store (SCREEN# rows)
                        │                                                    └──► live chunks to watchers' user channels
                        └─ existing mirror: hooks + transcript ──► relay ──► EVENT# rows (phone, agents)
```

### 5.1 The holder

One small process per shared session. It owns the pty and runs the provider in it. It does
nothing else: no network, no auth, no policy, no session bookkeeping. (Happy's daemon pain
came from a daemon that starts sessions, updates itself and tracks state; this one only
holds a terminal open.)

- **Binary location.** Never run from inside `Lightbulb.app`: the updater replaces the bundle
  and restarts the app (`lib.rs`, `UpdaterExt`). The app copies the holder binary to
  `~/Library/Application Support/company.thelightbulb.desktop/holder/<version>/` and spawns
  it from there, detached (`setsid`). Old holders keep running after an update.
- **Socket.** `/private/tmp/lightbulb-sessions-<uid>/holders/<holderId>.sock`, mode 0600, in
  the directory the app already uses for local session sockets (`local_sessions/enrollment.rs`).
  Every connection is checked: peer uid equals ours.
- **Manifest.** `holders/<holderId>.json`: `{holderId, protocol, pid, childPid,
  childBirthMicros, provider, cwd, conversationId?, startedAt}`. The app adopts running
  holders on launch by reading manifests and checking the pid. A dead pid's manifest is removed.
- **Protocol** (version 1, frozen; additive only). Length-prefixed frames:
  `hello{protocol, role}`, `output{bytes}`, `input{bytes}`, `resize{cols, rows}`,
  `exit{code|signal}`, `replay{bytes}`. Several clients may attach; all receive output, any
  local client may send input. The pty size follows the most recently active local client.
- **Ring buffer.** The last 256 KiB of output, sent as `replay` to a client that attaches, so
  the panel redraws after an app restart, and the recorder can fill the gap it missed.
- **Lifetime.** The holder exits when the child exits, after delivering `exit` to attached
  clients. Closing the panel or the outside terminal detaches; it does not end the session
  (open question 1).
- **Signals.** It forwards nothing from clients except as bytes (Ctrl-C is the `\x03` byte the
  TUI handles itself). SIGTERM to the holder terminates the child's process group.

### 5.2 Desktop app

- **Holder manager** (Rust): starts holders (folder + provider + args), adopts them on
  launch, keeps one attached connection per holder for recording, and exposes the list to the
  UI. The provider is started through the user's login shell (`zsh -l -c`), as
  `session_start.rs` already does for PATH.
- **Terminal panel** (web view): a new `PanelKind` `"terminal"` in
  `apps/frontend/src/components/workspace/panelRegistry.ts`, rendering xterm.js fed over
  Tauri IPC. New-session sheet: folder picker (approved folders), Claude Code or Codex.
- **Recorder**: for every attached holder, output is cut into chunks (at most 1 s or 12 KiB of
  raw bytes, whichever first; each chunk carries the current `cols`, `rows`) and sent as a new
  relay frame `local.session.screen` (§5.5). A gap the ring buffer cannot cover is sent as a
  `gap` chunk so viewers show "some output was not recorded".
- **Remote start and pick-up** (`session_start.rs`, `session_pickup.rs`) start a held session
  shown in the panel, not Terminal.app.

### 5.3 The `lightbulb` command

A subcommand of the app binary, `lightbulb-desktop cli`, installed on PATH through an
"Install `lightbulb` command" action that writes `/usr/local/bin/lightbulb` with an admin
prompt (the pattern VS Code uses for `code`).

`lightbulb claude [args]` / `lightbulb codex [args]` asks the running app (over the local
socket directory) to start a held session in the current folder with those args, then
attaches the terminal: raw mode, SIGWINCH → `resize`, bytes both ways, terminal restored on
every exit path. It prints one line first: `● Shared with <workspace>. Run plain \`claude\`
to keep a session private.` If the app is not running it says so and exits non-zero; it does
not start an unshared session silently.

### 5.4 What counts as shared

The holder manager knows each holder's `childPid`. A provider process is shared iff its pid
(+ birth time) is a held child. Everything else is ignored:

- Discovery (`claude agents --json`, Codex `ps`/`lsof`) and hooks keep running, because they
  give the session id and status, but their results are filtered to held pids before a
  mirror is created. Nothing about a private session is registered or uploaded.
- An environment marker is not used for this: it is inherited by a `claude` started inside
  a held session's shell (`local_sessions/start_links.rs:86-87` notes the same problem).
- The user-scope MCP server and skill stay installed. A private session's `send_message` is
  refused because attestation requires a mirrored process (`native_tools.rs`); read tools
  stay available.

Identity is unchanged: backend session id = uuid5(owner, device, provider, profile,
conversation); generation = the provider's pid + birth time. A resumed conversation keeps its
session id and gets a new generation through the existing successor chain.

### 5.5 Recording and the live view

- **Wire.** `local.session.screen {sessionId, generation, afterSeq, chunks:[{seq, ts, cols,
  rows, kind: "output"|"gap", data(base64)}]}` on the existing local-session relay, acked like
  event batches, ≤16 KiB per frame (backend `MAX_ENVELOPE_BYTES`). Its rate is independent of
  text event batches.
- **Storage.** One row per chunk, sort key `SCREEN#{seq:020d}`, in the session's own
  partition `LOCALSESSION#<owner>#<session>` (`local_sessions_repo.session_pk`) beside its
  `EVENT#` rows. `local_sessions_repo.delete_owner` already deletes every row under that
  prefix, so the account and workspace wipes cover recordings with no new code. Kept as long
  as the session row.
- **Live.** Viewers send `POST /messaging/sessions/{id}/viewing` every 15 s; the backend keeps
  a 45 s viewer set (the Mac live-view pattern, `mac_live.py`). On ingest it publishes each
  chunk to each current viewer's existing user channel (`live_events.publish_user`). No new
  AppSync namespace. Membership is rechecked on every heartbeat.
- **Viewer.** `GET /messaging/sessions/{id}/screen?afterSeq=&limit=` pages chunks. The web
  client replays them into a read-only xterm.js at the recorded size, scaled to fit, then
  applies live chunks in `seq` order; a gap in `seq` refetches. While the Mac is offline the
  last screen stays with an "Offline" note; after the end, the full recording is scrollable.
- **Volume.** Measured ~0.9 KB/s while Claude streams a reply. A 5-hour session is a few MB.
  Snapshots for fast late join are not built until replay is measured to be slow.

### 5.6 The structured record

Unchanged mechanism (hooks + transcript tail + `EVENT#` rows), restricted to held sessions.
It still feeds the phone, agents (`lightbulb_local_sessions`), room @session, approval
cards, delivery receipts and offline send. #3614 (per-session pacing, main thread before
subagents) is required.

### 5.7 Resume

- New command kind `"resume"` in `local_sessions.py` (`kind: Literal["send","interrupt",
  "approve"]` at line 414), carrying an optional first message.
- **Who:** the owner, and humans `may_control` / in `allowedSenders` for that session. Not
  agents. `allowed_senders` currently returns `[]` once `endedAt` is set
  (`local_sessions.py:665`); it must keep the list for Resume.
- **Where:** only the Mac that ran the session, same provider binary, same folder, no new
  flags or permission mode (the existing remote-start limits: approved folder, closed
  flags). Desktop consent from pairing applies.
- **How:** the holder manager starts `claude --resume <conversationId>` (Codex:
  `codex resume <threadId>`) in a new holder, shown in the panel. Once the successor
  generation registers, the first message is delivered through the normal send path with the
  sender's label.
- **Mac offline:** the command waits up to 24 h, like offline send (`OFFLINE_SEND_SECONDS`).
- **Owner is told:** a notice "<Name> resumed <title>" with Open and Stop it.
- **Clients:** web, desktop and iPhone show "Resume and send" on an ended session.

### 5.8 Phone

No terminal view. The session view, approval cards and message box as today, plus Resume.

## 6. Security and privacy

- **No remote keystrokes.** Nothing remote writes to a pty in this spec. Text from teammates,
  the owner's phone and agents goes through Claude's peer socket and `codex queue` with the
  existing labels; prompts are answered only by the `PermissionRequest` hook for the exact
  tool call. A later remote Stop must be a fixed `Esc` byte, owner or team control only,
  sent only when hooks say a turn is running and no prompt is waiting.
- **Recording is unredacted.** It shows anything on screen, including printed secrets and
  `!` shell output. Accepted by the owner because sharing is explicit. Readers are the same
  as for the session record (workspace members); agents do not get it.
- **Local sockets** are uid-checked; the CLI cannot attach to another user's holders.
- **Terms.** The unmodified Claude Code binary runs with the user's own login, which
  Anthropic's legal page does not restrict ("Nor does it prevent an end user from signing in
  to the unmodified Claude Code binary with their own Claude subscription").

## 7. Failure modes

| Event | Result |
|---|---|
| App restarts or auto-updates | Holders keep running from outside the bundle. The app re-adopts them; the panel redraws from the ring buffer; recording fills the gap or marks it. |
| App crashes | Same as restart. |
| Holder crashes | The session dies (same as a terminal crashing). The mirror records the end. |
| Outside terminal closed | The CLI detaches; the session keeps running and stays in the app. |
| Mac sleeps | Nothing is lost locally; viewers see "Offline" and the last screen. |
| Relay down | Chunks queue in the recorder up to a bound (8 MiB per session), then become a `gap`. |
| Old holder, new app | The app speaks every holder protocol version it ever shipped (one so far). |
| Two clients typing | Both reach the pty, as in tmux. Only local clients exist. |

## 8. Migration

On the release that turns this on, discovered sessions that are not held are no longer
registered; existing mirrors of them are ended. The desktop app shows once: "Sessions are
now shared only when you start them in Lightbulb or with `lightbulb claude`." No flag and no
compatibility path (owner ruling on flags and compat, 2026-09-12).

## 9. Later, not in this spec

Remote Stop (fixed `Esc`, owner-only), slash commands from remote surfaces, agents reading
the current screen as text, recording snapshots, a terminal view on the phone, Windows and
Linux.

## 10. Kill tests before building

1. A holder survives a real signed auto-update, and the app re-attaches and redraws Claude
   Code correctly.
2. xterm.js in WKWebView is good enough for all-day use: Shift+Enter, Option as Meta, Cmd
   shortcuts, paste, image paste/drop, IPC throughput, memory over 8 hours.
3. `claude` started by Lightbulb works as in a terminal: Keychain subscription login,
   folder-access prompts for repos under `~/Desktop`, login-shell PATH, git and ssh signing.
4. Codex in a holder: renders, is discovered for the record, `codex queue` delivery works.
5. Esc and the existing hooks behave the same under a holder against a real terminal
   emulator (not the simulated one used on 2026-10-02).

Also measure: recording volume and replay time for a long real session.

## 11. Open questions

1. Does closing the panel detach (session keeps running) or end the session?
2. Install path for `lightbulb` when the user declines the admin prompt (`~/.local/bin`?).
3. Recording retention: as long as the session row, or a fixed period?
4. Should Resume by a teammate need the owner's Mac to be unlocked or the owner present?

## 12. Evidence

Live tests on the founder's Mac, 2026-10-02 (Claude Code 2.1.287/288, Codex 0.159.3):

- A real `claude` in a pty owned by another process registered as an ordinary interactive
  session and was mirrored unchanged.
- A prototype pass-through relay: correct width, resize followed, slash commands by
  keystroke ran (and saved `/model` and `/effort` as the user's defaults: a hazard), an
  injected `Esc` stopped a turn cleanly, ~0.9 KB/s during a reply.
- `SIGINT` to an interactive `claude` closes the whole session.
- `claude --resume <id>` from a new process kept the conversation.

Research: Happy Coder source (kill-and-relaunch; no pty; daemon is its #1 complaint),
Superset (real PTYs held by a host service, headless emulator snapshots), VibeTunnel (`vt`
wrapper, recording, snapshots), Anthropic's legal and Agent SDK pages.
