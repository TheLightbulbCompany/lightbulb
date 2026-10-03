# Managed Session Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner code in a real Claude Code / Codex terminal inside the Lightbulb desktop app, share exactly the sessions started through Lightbulb, let teammates watch the real terminal while it runs, and let the owner or an admitted teammate resume an ended session from any device.

**Architecture:** A detached holder process per session owns a pty running the unmodified provider, keeps a terminal model (snapshot, modes, probe answers) and an offset-addressed output log. The desktop app attaches a local-assets terminal window and a recorder; while someone watches, a snapshot and live output go over the existing local-session relay to those viewers; nothing is stored. The existing transcript mirror stays, restricted to held sessions, for the phone and agents. Resume has its own rail through the device heartbeat, like pick-up.

**Tech Stack:** Rust (Tauri 2, `portable-pty`, a terminal emulator crate), TypeScript/React (Next.js web, `@lightbulb/shared`, Expo iOS), xterm.js, Python/FastAPI, DynamoDB, S3, AppSync Events.

**Spec:** `docs/superpowers/specs/2026-10-02-managed-session-sharing.md` (revision 2)

## Scope and order

Five subsystems, and Phase 1 decides facts the rest depends on (which emulator crate, whether the holder can run from the bundle, how macOS treats a held `claude`). This plan details **Phase 0 and Phase 1** task by task and lists Phases 2-6 with deliverables and tests. Each of Phases 2-6 gets its own detailed plan after Phase 1 reports.

| Phase | Ships | Depends on |
|---|---|---|
| 0 | #3614: the mirror keeps up | — |
| 1a | Standalone kill tests (holder, emulator, probes, Codex, Esc, xterm.js in WebKit) | — |
| 1b | Real-app kill tests (update survival, macOS identity, volume) | 1a, open question 4 |
| 2 | Holder + local terminal window, local only | 1b |
| 3 | Explicit sharing (held-only mirrors, hook filter, server refusal, migration) | 2 |
| 4 | Teammates watch a running terminal (live only, nothing stored) | 3 |
| 5 | Resume (desktop, backend, web, iPhone) | 3 |
| 6 | `lightbulb` command | 2 |

## Global Constraints

- Provider binaries run unmodified and interactive: no `-p`, `--bare`, stream-json, Agent SDK or `codex app-server` (spec §2.2).
- Nothing remote writes into a pty. Only the local terminal window's own webview and `lightbulb claude` send input (spec §5.2, §6).
- Shared iff the provider descends from a holder (Claude: first claim; Codex: thread id via the hook). Never an environment marker (spec §5.4).
- Relay frames ≤ 16 KiB including the envelope (`MAX_ENVELOPE_BYTES`, `core/services/local_sessions.py:50`); live chunks carry ≤ 11 KiB raw.
- Screen traffic never writes the session META row (spec §5.5).
- No feature flags and no compatibility paths (owner ruling 2026-09-12), unless the owner allows the founder-only first build (spec open question 4).
- Tests are written with the code and run in CI (`turbo run test`, backend pytest, `test-desktop` on the self-hosted Mac). Do not run full suites locally. Desktop behaviour is accepted on prod as the founder after promotion.
- Spike harnesses live under `scratch/managed-sessions/` on `spike/managed-sessions` and are never merged.
- Never type config-changing slash commands (`/model`, `/effort`, `/config`, `/permissions`, `/login`) into a real session in a test: they save as the user's defaults.

## Review Focus

1. **Multi-line paste into a window attached after >1 MiB of output**: expect it to arrive as one paste, not submit early. (Task 1.2; Phase 2 test.)
2. **A session started with no window attached** (remote start, Resume): expect it to start normally and draw correctly when a window attaches. (Task 1.1 Step 5; Phase 2 test.)
3. **A `claude` started inside a held session's shell**: expect it to stay private. (Phase 3 test.)
4. **A teammate opens a session that has been running for hours**: expect the current screen within a second or two (a snapshot, no history). (Phase 4 test.)
5. **A teammate resumes while the owner's Mac is asleep**: expect it to wait, run on wake in `plan` mode, and the message to arrive at most once, with "unknown" shown if its outcome is lost. (Phase 5 test.)

---

## Phase 0: the mirror keeps up

### Task 0.1: Land #3614

**Files** (branch `fix/session-mirror-freshness`, worktree `~/Desktop/lb-mirror-fresh.nosync`):
- Modify: `apps/desktop/src-tauri/src/local_sessions/transport.rs` (per-session spacing from the last ack)
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (main thread before subagents, 32-event round-robin budget)
- Modify: `apps/frontend/src/components/messaging/NativeSessionTranscript.tsx`, `packages/shared/src/messaging/localSessionTranscript.ts`
- Test: `local_sessions_own_thread_is_read_ahead_of_subagents_and_subagents_take_turns`

- [ ] **Step 1:** Add the reviewer's comment above `*next = Some(id.clone());` in `read_subagents`:

```rust
            // Advance even when this subagent has nothing new: the next refill must start
            // after it, or a quiet early id would be visited first forever.
            *next = Some(id.clone());
```

- [ ] **Step 2:** Confirm the reap loop removes `last_upload` entries for reaped keys: `last_upload.remove(&key);` must sit beside `sent_batches.remove(&key);` there (reviewer note; already in the diff).
- [ ] **Step 3:** Rebase on `origin/main`, push, take the PR out of draft, read Codex's review on all four surfaces (`reviews`, `pulls/<n>/comments`, `issues/<n>/comments`, `issues/<n>/reactions`).
- [ ] **Step 4:** Merge when CI is green and Codex's findings are handled.
- [ ] **Step 5:** After the next desktop release, on the founder Mac, every busy session's mirror is under 64 KB behind its transcript:

```bash
python3 - <<'EOF'
import glob, json, os
d = os.path.expanduser("~/Library/Application Support/company.thelightbulb.desktop/local-sessions")
for p in glob.glob(d + "/*/*.json"):
    m = json.load(open(p)); t = m.get("native", {}).get("transcript")
    if t and os.path.exists(t) and m.get("native", {}).get("status") == "busy":
        print(m["native"].get("title"), "behind KB:", (os.path.getsize(t) - m["cursor"]["offset"]) // 1024)
EOF
```

---

## Phase 1a: standalone kill tests

Branch `spike/managed-sessions` from `origin/main`. Each task ends by writing its result into spec §12 and committing that edit. A failure stops the plan for an owner decision.

### Task 1.1: Holder prototype with terminal model, offsets and probe answers

**Files:**
- Create: `scratch/managed-sessions/holder/Cargo.toml`, `scratch/managed-sessions/holder/src/main.rs`
- Create: `scratch/managed-sessions/attach.py`
- Modify: spec §12

**Interfaces (produced, reused by Phase 2):**
- Frames: `[u8 type][u32 len][payload]`; types `1 hello(json)`, `2 output(u64 offset + bytes)`, `3 input(bytes)`, `4 resize(u16 cols, u16 rows)`, `5 exit(json)`, `6 replay(u64 fromOffset)`, `7 gap(u64 from, u64 to)`, `8 snapshot(bytes)`, `9 detached`.
- `hello` JSON: `{"protocol":1,"pid":..,"pidBirth":..,"provider":"claude","cwd":"..","providerPid":..,"providerBirth":..}`.

- [ ] **Step 1: Cargo.toml**

```toml
[package]
name = "lb-holder-spike"
version = "0.0.0"
edition = "2021"

[dependencies]
portable-pty = "0.8"
vt100 = "0.15"
nix = { version = "0.29", features = ["process", "signal", "term"] }
serde_json = "1"
```

- [ ] **Step 2: Holder.** Write `src/main.rs` with: double-fork + `setsid`; `portable-pty` `native_pty_system().openpty(PtySize{rows:40, cols:120, ..})`; spawn `$SHELL -l -i -c "exec <argv>"` with `TERM=xterm-256color`, `COLORTERM=truecolor`, `LANG` defaulting to `en_US.UTF-8`; a `vt100::Parser` fed every output byte; a 1 MiB `VecDeque<u8>` log plus a `u64` total-offset counter; a `UnixListener` at the path in `argv[1]` with a uid check (`nix::sys::socket::getsockopt(fd, PeerCredentials)` or `getpeereid`); one interactive client slot and any number of output-only clients (role in the client's first frame: `hello{"role":"interactive"|"recorder"}`); on interactive attach send `snapshot(parser.screen().contents_formatted())` followed by the mode restore bytes built from `parser.screen()` mode getters (alt screen, bracketed paste, application cursor, mouse); `replay{from}` answers from the log or `gap`; exit when `child.wait()` returns.

- [ ] **Step 3: Probe answers.** Before forwarding output to clients, scan it for these queries and write the fixed answer to the pty master yourself; drop the same reply patterns arriving from clients:

```rust
const PROBES: &[(&[u8], &[u8])] = &[
    (b"\x1b[c", b"\x1b[?62;22c"),                 // DA1
    (b"\x1b[>0q", b"\x1bP>|xterm(lightbulb)\x1b\\"), // XTVERSION
    (b"\x1b[?u", b""),                            // kitty keyboard query: no answer = unsupported
    (b"\x1b]10;?\x1b\\", b"\x1b]10;rgb:e6e6/e6e6/e9e9\x1b\\"), // OSC 10 fg
    (b"\x1b]11;?\x1b\\", b"\x1b]11;rgb:1616/1616/1a1a\x1b\\"), // OSC 11 bg
];
```

Also match the `BEL`-terminated forms of OSC 10/11. Record any probe Claude Code or Codex sends that this table misses (log unknown `CSI ... c|q|n` and `OSC ...;?`).

- [ ] **Step 4: attach.py.** Raw mode, `hello{"role":"interactive"}`, stdin → `input`, `snapshot`/`output` → stdout, SIGWINCH → `resize`, restore termios in `finally`.

- [ ] **Step 5: Run.** (a) Start the holder with `claude` in a scratch repo with no client attached; wait 15 s; attach. Expected: Claude's welcome screen drawn correctly. (b) Start a turn ("Write the numbers 1 to 300, one per line, nothing else"); `kill -9` attach.py; reattach. Expected: current screen, `pgrep` shows the same provider pid. (c) Produce >1 MiB of output (`!seq 1 300000`), reattach, paste three lines from the clipboard. Expected: one paste, not three submits. (d) Log every `output` frame size and timestamp for the run.

- [ ] **Step 6:** Record results (including unknown probes and output volume) in spec §12; commit `docs(spec): holder spike results`.

### Task 1.2: xterm.js in WebKit

**Files:**
- Create: `scratch/managed-sessions/web/index.html` (xterm.js 5 + fit addon from cdnjs, pinned versions), `scratch/managed-sessions/web/bridge.py` (`websockets` server: one browser connection ↔ one holder client connection, binary frames passed through)
- Modify: spec §12

- [ ] **Step 1:** Write `bridge.py` (≈30 lines): accept a WebSocket on `localhost:8765`, open the holder socket as `interactive`, relay frames both ways.
- [ ] **Step 2:** Open `index.html` in **Safari** (WebKit, the engine of Tauri's WKWebView) against a held `claude`.
- [ ] **Step 3:** Check and record each: Shift+Enter inserts a newline in Claude's prompt; Option+B/F word jumps (xterm `macOptionIsMeta: true`); Cmd+C/Cmd+V; three-line paste after >1 MiB of output arrives as one paste; dragging a PNG attaches an image as in Terminal.app; `/` menu and arrows work; window resize redraws without duplicated scrollback.
- [ ] **Step 4:** Record results; commit.

### Task 1.3: Codex in a holder

**Files:** Modify spec §12.

- [ ] **Step 1:** Start the holder with `codex` in a scratch repo; attach; send "Reply with exactly: HI".
- [ ] **Step 2:** Check: TUI renders; the Codex `SessionStart` hook process's parent chain (`ps -o ppid= -p <pid>` repeatedly) reaches the holder pid; a rollout with `source: "cli"` appears; the installed Lightbulb app mirrors it (a state file in `local-sessions/<hash>/` containing the thread id); `codex queue --thread <id> --message "second"` is delivered.
- [ ] **Step 3:** Record results; commit.

### Task 1.4: Esc against a real terminal

**Files:** Modify spec §12.

- [ ] **Step 1:** In Terminal.app via attach.py, start a long Claude turn; send `\x1b` through a second, test-only client. Expected: "Interrupted", idle, transcript row containing `interrupted by user`.
- [ ] **Step 2:** Repeat with a permission prompt open; record what happens.
- [ ] **Step 3:** Record results; commit.

## Phase 1b: real-app kill tests

Needs the holder inside a real signed build, so it waits for the owner's answer to spec open question 4. Build the Task 1.1 holder into the app as a bundled, signed resource; the panel can be a bare local-assets window with xterm.js.

### Task 1.5: Update survival

- [ ] **Step 1:** Start a held `claude` from the app (holder running from inside the bundle); start a long turn.
- [ ] **Step 2:** Trigger a real update through the app's updater (`download_and_install` → `app.restart()`, `lib.rs:1367-1382`).
- [ ] **Step 3:** Expected: the provider pid is unchanged; the new app finds the holder and the window redraws from a snapshot; the turn finishes. If the holder died, repeat with the binary copied to Application Support and record which works.
- [ ] **Step 4:** Record; commit.

### Task 1.6: macOS identity

- [ ] **Step 1:** In a held `claude` started by the app: the banner shows the subscription ("Claude Max"); `which node git gh`, `nvm current`, `locale` match Terminal.app; a Unicode-heavy file renders.
- [ ] **Step 2:** Quit the app; then have the held session make its first read under `~/Desktop`. Record any prompt and whether the read succeeds. Repeat after Task 1.5's update.
- [ ] **Step 3:** `git commit -S --allow-empty -m test` in a scratch repo and `ssh -T git@github.com` succeed.
- [ ] **Step 4:** Record; commit.

### Task 1.7: Volume and long-session open

- [ ] **Step 1:** From Task 1.1's log and one real working hour in the app, record total bytes and peak bytes per second.
- [ ] **Step 2:** Time showing a five-hour session from its last keyframe in Safari (Task 1.2 page fed the snapshot bytes). Target: under 3 s.
- [ ] **Step 3:** Record; commit.

---

## Phase 2: holder and local terminal window

- `apps/desktop/src-tauri/src/holder/`: `protocol.rs` (Task 1.1 frames), `manager.rs` (start, find via sockets + `hello`, unlink dead, keep recorder attached), `bin/lightbulb-holder.rs` (productionized Task 1.1), bundled and signed in `tauri.conf.json` `resources`.
- Terminal window: a separate Tauri webview loaded from bundled assets (xterm.js), with a new capability file granting it alone `holder_start`, `holder_list`, `holder_attach` (Channel), `holder_input`, `holder_resize`. The main webview gets only `terminal_open{holderId?}`.
- New-session sheet (approved folder, Claude Code or Codex) reachable from the main app.
- `session_start.rs` / `session_pickup.rs` launch through the holder manager, bring the window to the front, post a native notification; delete `start_links.rs` and the `LIGHTBULB_SESSION_*` environment.
- Tests: frame codec round-trip; 200 KiB bracketed paste byte-for-byte through `input`; replay from an offset older than the log returns `gap`; a refused socket is unlinked; the main webview capability cannot invoke `holder_input`.

## Phase 3: explicit sharing

- Discovery filter at `transport.rs:849-858`: Claude by parent chain + first claim; Codex by `{holderId → threadId}` recorded by the hook.
- Hook binary: walks its parent chain; no holder → exit 0 immediately, nothing sent to the app.
- `local.session.register` carries `holderId`; backend refuses registrations without it and ends unheld sessions on deploy; delete the Claude `background` path (`claude.rs:25,165`).
- One-time desktop notice; edit the parity plan and the one-MCP spec per spec §3.
- Tests: plain `claude` not mirrored; held one mirrored; a grandchild `claude` not mirrored; a private session's `PermissionRequest` hook returns without waiting; register without `holderId` refused.

## Phase 4: teammates watch a running terminal

No recording (owner ruling 2026-10-03): running = watchable live; not running = the existing session view.

- Backend: `POST /messaging/sessions/{id}/viewing` (`VIEWER#<user>` row, 45 s TTL); watched session ids in the device-heartbeat ack; `local.session.screen` handler (row checks, no META write, nothing stored, publish to viewers' user channels off the ack path, dropped while `unshared`, 2 MiB/min bound); new user live event type in `USER_LIVE_EVENT_TYPES` and the shared contract.
- Desktop: a live relay per watched holder (attach output-only, snapshot first, chunks ≤ 11 KiB raw, a snapshot when the viewer set grows and every 10 s); stops when nobody watches.
- Web: `SessionTerminalView.tsx` (read-only xterm.js from snapshot + live frames, scaled) as the default view of a running held session; the existing session view when it is not running; message box, Ask to join and Resume under it.
- Tests: frame over 16 KiB rejected; frames dropped while paused; ended or unenrolled stops the push; agent, room-scoped and MCP callers refused on `/viewing`; nothing is sent while nobody watches; a late viewer gets a snapshot.

## Phase 5: Resume

- Backend: `_ended` copies `allowedSenders` to `resumeAllowed` (`local_session_relay.py:964-970`); owner can revoke it on an ended row; `POST /sessions/{id}/resume` writes `RESUME#` (conditional, 24 h) and `RESUMEWANT#<device>#<sid>` delivered in the device-heartbeat ack like `session_pickup.ack_with_pickup`; on the successor's register, seed the resumer into `allowedSenders` in the same transaction and admit the first message as a send.
- Desktop: handle `RESUMEWANT` (dedupe by `resumeId`; refuse if a held session owns the conversation); start `claude --resume <id>` / `codex resume <threadId>` in a holder in `plan` mode for non-owners or the recorded mode capped at `default` for the owner; window to front; notification with a local Stop it.
- Web and iPhone (`apps/mobile/src/app/(app)/session/`): "Resume and send" on ended sessions, with its own predicate beside `sessionControls.ts:32`.
- Tests: teammate without send rights refused; agent refused; second resume while one is pending refused; offline resume runs on the next heartbeat; first message sent at most once, lost outcome surfaced as unknown; non-owner resume starts in `plan`.

## Phase 6: the `lightbulb` command

- `apps/desktop/src-tauri/src/main.rs`: `cli` subcommand; asks the app to start a held session in `$PWD`, prints the shared line, attaches as the interactive client, restores the terminal on exit, exits with the child's code.
- Install: symlink `~/.local/bin/lightbulb`; show the PATH line if needed.
- Tests: terminal restored after the child exits and after Ctrl-C; app not running → message and exit 1; attaching from the CLI detaches the window as the interactive client.
