# Managed Session Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner code in a real Claude Code / Codex terminal inside the Lightbulb desktop app, share exactly the sessions started through Lightbulb, show teammates the same real view (live and recorded), and let the owner or an admitted teammate resume an ended session from any device.

**Architecture:** A tiny detached holder process per session owns a pty running the unmodified provider. The desktop app attaches to holders for its terminal panel and records their output to the existing local-session relay; the backend stores chunks beside the session's events and pushes them to watching teammates' user channels. The existing transcript mirror stays, restricted to held sessions, for the phone and agents.

**Tech Stack:** Rust (Tauri 2 desktop, `apps/desktop/src-tauri`), TypeScript/React (Next.js web, `@lightbulb/shared`, Expo iOS), xterm.js, Python/FastAPI backend, DynamoDB, AppSync Events.

**Spec:** `docs/superpowers/specs/2026-10-02-managed-session-sharing.md`

## Scope and order

The spec spans five subsystems, and the first two phases decide facts the rest depends on (which holder, whether xterm.js in WKWebView is good enough, how macOS attributes a held `claude`). So this plan details **Phase 0 and Phase 1 task by task**, and lists Phases 2-6 with their deliverables, files and acceptance. Each of Phases 2-6 gets its own detailed plan, written after Phase 1 reports, in this file's format.

| Phase | Ships | Depends on |
|---|---|---|
| 0 | #3614: the mirror keeps up | — |
| 1 | Kill-test results written into the spec §12 | — |
| 2 | Holder + terminal panel, local only (no sharing change) | 1 |
| 3 | Explicit sharing: only held sessions are mirrored; migration notice | 2 |
| 4 | Recording + teammate terminal viewer | 3 |
| 5 | Resume (desktop, backend, web, iPhone) | 3 |
| 6 | `lightbulb` command for outside terminals | 2 |

## Global Constraints

- Provider binaries run unmodified: no `-p`, no `--bare`, no stream-json, no Agent SDK, no `codex app-server` (spec §2.2).
- Nothing remote writes into a pty. Remote text uses Claude's peer socket and `codex queue` with existing labels; prompts are answered only by the `PermissionRequest` hook (spec §6).
- The holder never runs from inside `Lightbulb.app` (spec §5.1).
- Shared iff the provider's pid + birth time is a held child; never an environment marker (spec §5.4).
- Relay frames ≤ 16 KiB (`MAX_ENVELOPE_BYTES = 16 * 1024`, `core/services/local_sessions.py:50`).
- No feature flags and no compatibility paths (owner ruling 2026-09-12). Exception to decide before Phase 2: see "Open decision" below.
- Tests: write them with the code; CI runs them (`turbo run test`, backend pytest, `test-desktop` on the self-hosted Mac). Do not run full suites locally. Desktop behaviour is accepted on prod as the founder after promotion; there is no dev desktop acceptance.
- Spike harnesses live under `scratch/managed-sessions/` on the spike branch and are never merged.
- Never type config-changing slash commands (`/model`, `/effort`, `/config`, `/permissions`, `/login`) into a real session in a test: they save as the user's defaults.

## Open decision (owner) before Phase 2

Phase 2 puts a new terminal panel in front of every desktop user on promotion. Options: (a) ship to everyone and accept rough edges; (b) reuse the existing owner allowlist pattern (`DESKTOP_EXECUTION_OWNER_ALLOWLIST`, CDK-derived from GitHub variables) to show "New session" to the founder first. (b) is a gate, which the no-flags ruling forbids unless the owner allows it here.

## Review Focus

1. **A wide paste or image drop into the panel** while Claude is busy: expect it to arrive whole, in order, exactly as in Terminal.app. (Phase 2 test: 200 KiB bracketed paste round-trips byte-for-byte through holder input.)
2. **The app restarts mid-turn**: expect the session to keep working and the panel to come back at the right size with the current screen, not a blank or garbled one. (Phase 2 test: re-attach after killing the app process; replay then resize redraws.)
3. **A `claude` started from inside a held session's shell tool**: expect it to stay private. (Phase 3 test: a grandchild provider pid is not shared.)
4. **A teammate opens a long session**: expect the recorded view within a few seconds, not a hang. (Phase 4 test: replay of 5 MB of chunks renders in under 3 s in the viewer.)
5. **Resume while the owner's Mac is asleep**: expect it to wait and run on wake, and the message to arrive once, not twice. (Phase 5 test: resume queued offline delivers exactly one send after the successor registers.)

---

## Phase 0: the mirror keeps up

### Task 0.1: Land #3614

**Files:** already on branch `fix/session-mirror-freshness` (worktree `~/Desktop/lb-mirror-fresh.nosync`):
- Modify: `apps/desktop/src-tauri/src/local_sessions/transport.rs` (per-session 500 ms spacing from the last ack)
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (main thread before subagents; 32-event subagent refill budget, round-robin)
- Modify: `apps/frontend/src/components/messaging/NativeSessionTranscript.tsx`, `packages/shared/src/messaging/localSessionTranscript.ts` (`sessionClock`, turn-start spinner)
- Test: `local_sessions_own_thread_is_read_ahead_of_subagents_and_subagents_take_turns`

- [ ] **Step 1:** Address the reviewer's note: add a comment above `*next = Some(id.clone());` in `read_subagents`:

```rust
            // Advance even when this subagent has nothing new: the next refill must start
            // after it, or a quiet early id would be visited first forever.
            *next = Some(id.clone());
```

- [ ] **Step 2:** Rebase on `origin/main`, push, take the PR out of draft, and read Codex's review on all four surfaces (`reviews`, `pulls/<n>/comments`, `issues/<n>/comments`, `issues/<n>/reactions`).
- [ ] **Step 3:** Merge when CI is green and Codex's findings are handled.
- [ ] **Step 4:** After the next desktop release, confirm on the founder Mac that the state files' `cursor.offset` stays within a few seconds of the transcript size during a session with running subagents:

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

Expected: every busy session behind by under 64 KB.

---

## Phase 1: kill tests

Branch: `spike/managed-sessions` from `origin/main`. Each task ends by writing its result (pass, fail, numbers, what surprised you) into spec §12 and committing that edit. A fail stops the plan for an owner decision.

### Task 1.1: Holder survives the app going away

**Files:**
- Create: `scratch/managed-sessions/holder/` (standalone Rust crate: `Cargo.toml`, `src/main.rs`)
- Modify: `docs/superpowers/specs/2026-10-02-managed-session-sharing.md` §12

- [ ] **Step 1: Write the holder.** One binary, `holder <socket> <manifest> -- <argv...>`: `setsid`, `openpty` (crate `nix`), spawn argv in the pty, listen on the unix socket, keep a 256 KiB ring buffer, frames `[u8 type][u32 len][bytes]` with types `1 hello, 2 output, 3 input, 4 resize, 5 exit, 6 replay`, write the manifest JSON `{pid, childPid, startedAt}`, exit after the child exits.

```rust
// src/main.rs (core loop shape; complete the error handling as you write it)
use nix::pty::{openpty, Winsize};
use std::os::unix::net::UnixListener;
const RING: usize = 256 * 1024;
fn main() {
    let args: Vec<String> = std::env::args().collect();
    let (sock, manifest, argv) = (&args[1], &args[2], &args[4..]);
    nix::unistd::setsid().ok();
    let pty = openpty(Some(&Winsize { ws_row: 40, ws_col: 120, ws_xpixel: 0, ws_ypixel: 0 }), None).unwrap();
    let child = std::process::Command::new(&argv[0]).args(&argv[1..])
        .stdin(pty.slave.try_clone().unwrap()).stdout(pty.slave.try_clone().unwrap()).stderr(pty.slave)
        .spawn().unwrap();
    std::fs::write(manifest, format!("{{\"pid\":{},\"childPid\":{}}}", std::process::id(), child.id())).unwrap();
    let listener = UnixListener::bind(sock).unwrap();
    // poll(master, listener, clients): master output -> ring + every client; client input -> master;
    // resize -> TIOCSWINSZ on master; on attach send `replay` with the ring; on child exit send `exit`.
    let _ = (listener, RING);
}
```

- [ ] **Step 2: Write the attach client** `scratch/managed-sessions/attach.py`: raw mode, relay stdin → `input`, `output`/`replay` → stdout, SIGWINCH → `resize`, restore the terminal on exit.
- [ ] **Step 3: Run the test.** In Terminal.app: start `holder` detached with `claude` in a scratch repo, attach, start a turn ("Write the numbers 1 to 300, one per line"), then `kill -9` the attach client (stands in for the app dying). Reattach.

Expected: `claude` is still running (`pgrep -f claude` shows the child pid from the manifest); the reattached screen shows the current state after replay plus one resize nudge (send `resize` with rows-1 then rows).

- [ ] **Step 4: Bundle replacement.** Copy the holder binary to `~/Library/Application Support/company.thelightbulb.desktop/holder/spike/`, run it from there, then replace a copy of `Lightbulb.app` in a scratch folder with another version using `mv` (what the updater does). Expected: the holder is unaffected. Record whether macOS showed any prompt.
- [ ] **Step 5:** Record results in spec §12; commit `docs(spec): holder survival results`.

### Task 1.2: xterm.js in WKWebView as an all-day terminal

**Files:**
- Create: `scratch/managed-sessions/panel/` (a minimal Tauri 2 app: one window, xterm.js 5 + fit addon from cdnjs, Rust side attaches to the Task 1.1 holder and streams `output` over a Tauri `Channel`, `input` via `invoke`)
- Modify: spec §12

- [ ] **Step 1:** Build the minimal app with `cargo tauri dev` (scratch only; not the Lightbulb app).
- [ ] **Step 2:** Check each item and record pass/fail: Shift+Enter inserts a newline in Claude Code's prompt; Option+B/F word jumps; Cmd+C/Cmd+V; Cmd+K clear; pasting 200 lines arrives whole (bracketed paste); dragging a PNG into the prompt attaches an image as in Terminal.app; `/` command menu and arrow keys work; resize the window: Claude redraws without duplicated scrollback.
- [ ] **Step 3:** Throughput: run `seq 1 200000` in a held shell; record time to finish and whether the window stays responsive.
- [ ] **Step 4:** Memory: leave a session running a long Claude task for 8 hours; record the WebContent process RSS hourly (`ps -o rss= -p <pid>`).
- [ ] **Step 5:** Record results; commit.

### Task 1.3: `claude` launched by Lightbulb behaves like a terminal's

**Files:** Modify spec §12.

This needs the real app identity, so it runs on the first Phase 2 build, as the founder, before any further Phase 2 work. Write it here so it is not skipped.

- [ ] **Step 1:** In a held session started by the app, check: Claude shows "Claude Max" (Keychain login read); a repo under `~/Desktop` opens without a folder-access prompt, or the prompt names Lightbulb once; `which node git gh` matches a login shell; `git commit -S` signs; `ssh -T git@github.com` works.
- [ ] **Step 2:** Record results; commit.

### Task 1.4: Codex in a holder

**Files:** Modify spec §12.

- [ ] **Step 1:** Start `codex` in the Task 1.1 holder from a scratch repo; send one prompt ("Reply with exactly: HI").
- [ ] **Step 2:** Check: the TUI renders; a rollout appears in `~/.codex/sessions/<date>/` with `source: "cli"`; the installed Lightbulb app creates a mirror for it (a state file in `local-sessions/<hash>/` containing the thread id); `codex queue --thread <id> --message "second"` is delivered.
- [ ] **Step 3:** Record results; commit.

### Task 1.5: Esc and hooks against a real terminal

**Files:** Modify spec §12.

- [ ] **Step 1:** In Terminal.app attached through `attach.py`, start a long Claude turn; send `\x1b` through a second client connection. Expected: "Interrupted", session idle, transcript row `[Request interrupted by user]`.
- [ ] **Step 2:** Repeat with a permission prompt open. Record what Esc does there (expected: rejects the prompt). This decides the state gate for the later remote Stop.
- [ ] **Step 3:** Record results; commit.

### Task 1.6: Recording volume

**Files:** Modify spec §12.

- [ ] **Step 1:** Have the holder log every `output` frame's size and timestamp during one real working hour of Claude Code use.
- [ ] **Step 2:** Record total bytes, peak bytes per second, and the time xterm.js takes to replay the whole log (feed it in one `write` call in the Task 1.2 app). Commit.

---

## Phase 2: holder and terminal panel (local only)

Detailed plan to be written after Phase 1. Deliverables:

- `apps/desktop/src-tauri/src/holder/` (new module): `protocol.rs` (frames v1), `manager.rs` (spawn from the copied binary, adopt from manifests, attach, list), `bin/lightbulb-holder.rs` (the Task 1.1 holder, productionized).
- `apps/desktop/src-tauri/src/lib.rs`: Tauri commands `holder_start{cwd, provider}`, `holder_list`, `holder_attach{holderId}` (Channel of output), `holder_input`, `holder_resize`.
- `apps/frontend/src/components/workspace/panelRegistry.ts`: `PanelKind` gains `"terminal"`; new `apps/frontend/src/components/workspace/panels/TerminalPanel.tsx` (xterm.js) and a "New session" sheet (approved folder, Claude Code or Codex).
- `session_start.rs` and `session_pickup.rs` start held sessions in the panel instead of Terminal.app.
- Tests: a 200 KiB bracketed paste round-trips byte-for-byte through holder `input`; after the app process is killed, re-attach gets `replay` and a resize redraw at the panel's size; a dead holder's manifest is removed on adoption.
- Acceptance (prod, founder): Phase 1 checklist passes in the real app; an update with a session running leaves it running and redrawn.

## Phase 3: explicit sharing

Deliverables:

- `apps/desktop/src-tauri/src/local_sessions/transport.rs` discovery path: create mirrors only for provider pids that are held children (from the holder manager); ignore hooks and listings for any other pid.
- Existing mirrors of unheld sessions are ended on first run of the new build; a one-time notice: "Sessions are now shared only when you start them in Lightbulb or with `lightbulb claude`."
- `docs/superpowers/plans/2026-10-01-native-session-parity.md` and `docs/superpowers/specs/2026-10-02-one-lightbulb-mcp.md` edited per spec §3.
- Tests: a grandchild `claude` of a held session is not mirrored; a plain `claude` is not mirrored; a held one is.

## Phase 4: recording and the teammate viewer

Deliverables:

- Desktop recorder: chunks (≤1 s or 12 KiB, with `cols`/`rows`, `gap` chunks) sent as `local.session.screen`, ack-gated, bounded queue 8 MiB per session.
- Backend: `local.session.screen` handler in `core/services/local_session_relay.py` writing `SCREEN#{seq:020d}` rows under `local_sessions_repo.session_pk`; `POST /messaging/sessions/{id}/viewing`; `GET /messaging/sessions/{id}/screen?afterSeq=&limit=`; live publish to current viewers via `live_events.publish_user`.
- Web: `apps/frontend/src/components/messaging/SessionTerminalView.tsx` (read-only xterm.js, scaled, seq-ordered, gap refetch, Offline and Ended states) as the default view of a held session; message box, Ask to join and Resume under it.
- Tests: chunk ordering and gap refetch; viewer removed from the workspace stops receiving within one heartbeat; account wipe deletes `SCREEN#` rows.

## Phase 5: Resume

Deliverables:

- Backend: `kind: "resume"` in `core/services/local_sessions.py` (line 414 Literal); authorization owner or `may_control` / `allowedSenders`; `allowed_senders` keeps the list after `endedAt` (line 665); 24 h offline wait; owner notice event.
- Desktop: resume handler starts `claude --resume <conversationId>` / `codex resume <threadId>` in a holder with the recorded cwd; delivers the first message after the successor generation registers.
- Web and iPhone: "Resume and send" on ended sessions (`apps/mobile/src/app/(app)/session/`).
- Tests: teammate without send rights is refused; offline resume delivers exactly once; agents cannot resume.

## Phase 6: the `lightbulb` command

Deliverables:

- `apps/desktop/src-tauri/src/main.rs`: `cli` subcommand (`lightbulb claude|codex [args]`): ask the app to start a held session in `$PWD`, print the shared line, attach (raw mode, SIGWINCH, restore on exit), exit with the child's code.
- "Install `lightbulb` command" action writing `/usr/local/bin/lightbulb` with an admin prompt.
- Tests: terminal restored after the child exits and after Ctrl-C; app not running → message and exit 1.
