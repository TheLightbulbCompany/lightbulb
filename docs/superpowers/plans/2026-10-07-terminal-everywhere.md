# Plan: the real terminal everywhere, then session chat, then browsers

Status: proposed 2026-10-07, not started. Delete when shipped (PR bodies are the record).
Specs: `../specs/2026-10-07-terminal-everywhere-design.md` (P1),
`../specs/2026-10-07-session-chat-design.md` (P2),
`../specs/2026-10-07-coding-agent-browser-design.md` (P3, P4; research only).

Rules for every slice: a fresh worktree on `origin/main`; one PR per slice; Codex reviews
(merge on Codex, fix real findings, merge through nitpicks); dev deploys on merge, prod by
dispatch; desktop and session-screen checks on PROD as the founder after a desktop build (no dev
desktop build exists); phone checks in the simulator and on the TestFlight build; a merge touching
`apps/mobile` or `packages/shared` ships an iPhone OTA. Hermes skill text (`surfaces.md`) changes
ride a pin PR plus a runtime roll (owner's yes). Subagents: Sonnet for scoped work, Opus for the
Rust holder/relay slices and final reviews.

## Coordination (before touching these files)
The local Codex task (worktrees under `~/.codex/worktrees/*`, branches `codex/session-*`,
`codex/updater-*`; its last PRs #3780–#3784, #3788, #3789) owns crashes, updater failures,
release verification and unfinished acceptance. Its files overlap ours: `local_sessions/codex.rs`,
`transport.rs`, `lifecycle.rs`, `core/services/local_session_relay.py`, `SessionFocus.tsx`,
`localSessionLabels.ts`, the mobile session screen, the updater routes. Before a slice touches one
of them: `git fetch && git branch -r | grep codex/`, read its open PRs, and keep our diff in new
functions/files where possible. No slice here dispatches a release; prod trains stay with the
owner's coordinator session.

## Owner decisions that gate slices
Agreed 2026-10-07: no pictures, no recordings, no mirrors, no Line mode; the shell switch on by
default; keys as in native Claude Code with a typist-coloured cursor; the ended screen lives while
the app runs. Nothing is open.

## P1 — Terminal everywhere

### S1. No transcript in the human view; explicit states (web + phone + skill text)
Files: `LocalSessionPanel.tsx`, `SessionTerminalView.tsx`, mobile `session/[sessionId].tsx`,
`packages/shared` labels, `surfaces.md` (+ MAINTENANCE anchors), rev 5 spec decisions 4 and 8.
- Remove `NativeSessionTranscript`, `SubagentPanel`, `usePendingPrompt`, `useHistorySource`
  from the human view (keep them for `/s`, Details, approvals; keep/retire table in the research
  report §4).
- New `SessionTerminalState` panel: status line + reason + the one action (switch / ← / Open in
  Lightbulb / Resume) for `terminal === null`, ended, offline; never event-derived rows.
- Remove the 30 s automatic transcript fallback; show "Reconnecting…" instead.
- Accept: on prod, a plain interactive Claude session opened by a teammate shows the explicit
  state and no rows from events; an ended session shows its final screen, or the "Ended" state after an app restart, plus Resume; the terminal
  of a held session is unchanged; `lightbulb_local_sessions read` still returns events (agent
  unaffected); Codex review clean.

### S2. History and viewer independence (desktop relay + web)
Files: `local_sessions/screen.rs`, `holder/panel.rs`, `session_screen.py` (`kind` enum),
`SessionTerminalView.tsx`, `sessionScreen.ts` → `packages/shared`.
- Relay: on a grown viewer set, send `history` frames (REPLAY of the last ≤256 KiB) then the
  snapshot; `ping` every 10 s instead of a full redraw when nothing changed; snapshot after
  resize/gap stays.
- Viewer: scrollback 5000; first snapshot resets, in-sync snapshots ignored, gap snapshot
  clears+redraws; stale = no frame/ping 30 s.
- Panel: `replayFrom` on HELLO.
- Accept (prod, founder Mac + Isol8 Admin in Chrome): a viewer opening a running session sees
  the last screens of history and can scroll them; scrolling, selecting, resizing the viewer
  window changes nothing on the owner's terminal (owner's screen compared before/after);
  a hidden tab brought back shows the picture within 2 s with history intact; the picture never
  swaps to a transcript; relay log shows no budget drops at idle.

### S3. Phone terminal (WebView)
Files: `apps/frontend/src/app/terminal/page.tsx` (static xterm page), mobile session screen,
`components/session/TerminalView.tsx`, key bar, Line composer.
- Accept (simulator against prod, then TestFlight): a held session renders the real terminal;
  frames arrive while foregrounded; pinch zoom only scales the phone; Line send lands as one
  prompt; Keys mode sends Esc/arrows; backgrounding and returning recovers within 10 s; approval
  cards and Ask to join still work; no transcript list anywhere.

### S4. Hosting by default
Files: `DesktopPermissionsGate.tsx`, `ExplicitSharingNotice.tsx`, `LocalSessions.tsx` copy,
`cli.rs` (`--bg` passthrough), pairing copy.
- Accept (prod, new pairing on a test account's Mac or re-pair on the founder Mac): after
  pairing, a new terminal tab running `claude` is a held session with a live terminal; the
  Settings switch shows on; turning it off removes the rc block; `lightbulb claude --bg` runs the
  real tool.

### S5. The ended screen and the offline state
Files: `holder/run.rs` (keep the socket, model and log after EXIT; refuse input; close on the
app's request), `holder/manager.rs`, `local_sessions.rs` (`terminal()` + `screenAvailable`),
`local_sessions.py` (`_session_public` keeps `terminal` on an ended held session while the
screen is available; `watch` admits it), web and phone states "Ended · <time>" + Resume and
"Mac offline" + Resume + Pick up.
- Accept (prod, founder Mac + a second account): end a held session → the final screen stays
  visible to the teammate with Resume beside it; Resume → the panel switches to the live
  terminal and the old holder closes; quit the app → the teammate sees "Mac offline" with
  Resume disabled and Pick up offered; relaunch → the ended screen is gone and the state says so;
  nothing is written under Application Support or S3 (checked).

### S6. Explicit states for unheld plain sessions
Files: desktop `terminalReason` (`plain`, `no-daemon`), web and phone state panel.
- Accept (prod): a plain `claude` started in a shell opened before the switch shows "Press ← in
  its terminal" to everyone and no transcript; pressing ← switches the panel to the attach
  terminal; a `codex --no-daemon` session shows "Start it again with `lightbulb codex`".

### S7. "Open in a terminal" for IDE and desktop-app sessions
Files: desktop discovery (`terminalReason: app` for VS Code / Cursor / JetBrains / Codex app /
Claude Desktop sessions), a `terminal_open` command that runs `start_held` with
`claude --resume <conversation>` / `codex resume <thread>`, web and phone state panel.
- Accept (prod, founder Mac): a Claude session started in VS Code's panel shows "Runs in VS Code,
  not in a terminal" to a teammate, with no transcript rows; the owner presses Open in a terminal
  → a held session with the same conversation appears live for both; a teammate has no such
  button; the VS Code session keeps running.

### S8. Typist-coloured cursor and presence in the Lightbulb views
Files: `local_sessions/screen.rs` (`typist` on frames for 4 s after a teammate's input),
`session_screen.py` (`Screen` model), `SessionTerminalView.tsx`, the desktop panel
`terminal.js`, the phone terminal page.
- Accept (prod, two accounts): when the teammate types, every Lightbulb view shows the cursor
  in the teammate's colour with their name for about 4 s, then the plain cursor returns; the
  owner's keys restore the plain cursor at once; the band still says "Aden typing"; no typing
  pause anywhere; the submitted prompt carries the teammate's label.

## P2 — Session chat (spec 2026-10-07-session-chat-design.md)
S9 backend room door + lifecycle hooks; S10 web second room slot + right column + notification
routing; S11 phone Chat button; S12 skill text. Accept: two accounts chat beside a running
session with independent scroll/draft; a chat member who is not admitted cannot type into the
terminal (input route 403); unsharing silences the chat for the teammate within 5 s; ending the
session archives the room; a mentioned agent answers in chat and never in the terminal.

## P3 / P4 — Browsers (research only; see the browser spec for the spikes)

## Pass / fail / blocked matrix (state today, 2026-10-07; to be re-run per slice)

| Case | Today | Verdict today | Slice |
|---|---|---|---|
| Started in Lightbulb, running, web/desktop viewer | real terminal | PASS (observed; prod-tested 2026-10-04 by the sessions track) | — |
| Same, earlier history / scrollback for a viewer | screen only | FAIL | S2 |
| Same, viewer hidden then shown | snapshot ≤10 s, history lost | FAIL | S2 |
| Plain `claude`, switch on | held | PASS (prod-tested 2026-10-04 on the founder Mac) | S4 makes it default |
| Plain `claude`, switch off / shell open before it | transcript | FAIL | S4 (default on), S6 (explicit state → ←) |
| `claude --bg` job | attach terminal | PASS (prod-tested 2026-10-04) | S2 for history |
| ← moved session | attach (fork) | PASS | — |
| Plain `codex` on the daemon | attach terminal | PASS (prod-tested 2026-10-04, holder closures seen 3× then stable) | S2 |
| `codex --no-daemon` outside Lightbulb | transcript | FAIL | S6 (explicit state) |
| VS Code / Cursor / JetBrains Claude panel | not tracked or transcript | FAIL | S7 (explicit state + Open in a terminal) |
| Codex desktop app threads | transcript | FAIL | S7 |
| Claude Desktop Code tab | not tracked | FAIL (no TUI exists) | S7 |
| Ended held session | transcript | FAIL | S5 (final screen while the app runs) |
| Mac offline / app quit | transcript | FAIL | S5 (explicit state; by design no screen) |
| Resumed session | transcript until the tool redraws | PASS once the tool draws; S1 removes the transcript | S1 |
| Any session on the iPhone | transcript | FAIL | S3 |
| Viewer resize changes another's view | never | PASS (observed) | — |
| Owner panel resize changes viewers' cols×rows | yes, by design (owner's terminal) | PASS by design | — |
| Two typists on one prompt | raw interleave, labelled by Enter, no cursor colour | PARTIAL | S8 |
| Automatic transcript fallback after 30 s stale | exists | FAIL (brief forbids) | S1 |
| Mod band, owner-only Allow/Decline, no typing pause | as ruled | PASS (prod-tested 2026-10-05) | — |
| Session chat beside the terminal | none ("Message owner" closes the session) | FAIL | P2 |
| Coding-agent browser view | none | BLOCKED (research only, last) | P3 |
| AgentCore browser via CLI/MCP | none | BLOCKED (research only, absolute last) | P4 |
