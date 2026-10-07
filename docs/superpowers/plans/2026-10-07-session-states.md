# Session States (S4, S5, S6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An ended held session keeps its final screen for viewers while the owner's app runs; a Mac that is offline, a plain `claude` and a `codex --no-daemon` each get an explicit state with one action; "Share every session live" is on by default at pairing.

**Architecture:** The holder stops exiting with its provider: after EXIT it keeps socket, model and log in memory, answers watchers with the final snapshot and refuses input. The desktop indexes such ended sessions as live for the existing screen relay (no `screen.rs` edit), tells the relay on the end frame and in a `screens` heartbeat frame, and reports `terminalReason` for a running session without a terminal. The backend stamps `screenUntil` (lapses 30 s after the Mac stops renewing), projects `terminal` + `screenAvailable` for an ended held session, and admits it to `watch`, the heartbeat ack and `forward`. The web keeps the terminal view mounted read-only under "Ended · <time>"; the phone gets the state copy only.

**Tech Stack:** Rust (Tauri desktop, holder process), Python 3 / FastAPI / DynamoDB (moto in tests), TypeScript (Next.js web, `@lightbulb/shared`, Expo phone), vitest, jest, pytest.

**Spec:** `docs/superpowers/specs/2026-10-07-terminal-everywhere-design.md` (rev 3) — §3 decisions 3 and 4, §4 rows f, g, j, k, l, §5.1, §5.4. Slice text and Accept lines: `docs/superpowers/plans/2026-10-07-terminal-everywhere.md` §S4, §S5, §S6.

## Global Constraints

- Spec rev 3 is the authority. Nothing in this plan records a screen: no file under Application Support, no S3 object, no DynamoDB copy of screen bytes. The ended screen lives in the holder's memory only.
- An ended held session keeps its final screen visible: the holder keeps socket, model and log after EXIT, refuses input and resizes, and closes when the app stops it (a Resume's new holder replaced it, its mirror was reaped, or the app relaunched). Web copy: `Ended · <timeAgo>` above the screen, with Resume (the existing `SessionResume`). When the screen is gone, S1's copy stays: "The screen is gone: this session's terminal closed."
- Offline (`status: "offline"`): "<Owner>'s Mac is offline" (S1, unchanged) + a disabled **Resume** + the existing **Pick up** door (`SessionPickup` in the status bar, `LocalSessionPanel.tsx:204`). Build no new pick-up door.
- `terminalReason`: `plain` → S1's ← lines (owner: "Press ← in its terminal to share this one live, or turn on Share every session live in Settings to make every new session live."; teammate: "Ask <owner> to press ← in their terminal."). `no-daemon` → owner: "Start it again with `lightbulb codex` to share its screen."; teammate: "Ask <owner> to start it again with `lightbulb codex`." Keep S1's title "This session was not started through Lightbulb, so its screen is not here." Add only the reason line.
- "Share every session live" defaults ON at pairing; turning it off removes the rc block (`shell_integration::set(home, false)`, already true); the pairing copy names the files it writes. `lightbulb claude --bg` runs the real tool. No per-operation prompt anywhere.
- Do not edit `local_sessions/screen.rs`, `holder/panel.rs`, `SessionTerminalView.tsx`. In `holder/manager.rs` only ADD functions. In `LocalSessionPanel.tsx` edit only the state-panel branches (copy and conditions, lines 72-76, 172-188, 242-245); CSS goes append-only at the end of `messaging.css`.
- `local_sessions/transport.rs` is touched at exactly six one-line seams (Task 2 lists them). `lifecycle.rs` is not touched. Before Tasks 2 and 3 run `git fetch && git branch -r | grep codex/` and read open `codex/session-*` PRs: the local Codex task also edits `transport.rs` and `core/services/local_session_relay.py`. Keep new logic in new functions.
- New relay fields go only to a relay that lists them: `FEATURES` gains `endedScreens` and `terminalReason`; the desktop sends `screens`, `screen` and `terminalReason` only after a registration ack lists them. Public JSON stays camelCase; `LocalSession` gains `screenAvailable?: boolean` and `terminalReason?: "plain" | "no-daemon" | "app" | "sdk" | "unknown"`.
- Test commands (no other form): Rust from the worktree root `CARGO_TARGET_DIR=/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/target TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml <--lib module | --test name>`; backend from `apps/backend`: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest <files> -q --no-cov`; web from `apps/frontend`: `pnpm vitest run <files>`, `pnpm exec tsc --noEmit`, `pnpm lint`; shared from `packages/shared`: `pnpm vitest run <files>`, `pnpm exec tsc --noEmit`; mobile from `apps/mobile`: `pnpm test -- <file>`, `pnpm typecheck`, `pnpm lint`.
- Commit trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **Resume while the ended holder still lives.** `resume::prepare` (`resume.rs:266`) treats any listed holder whose argv names the conversation as "already running" and starts nothing; a Codex holder's `.thread` file would bind a resumed thread to the dead holder (`local_sessions.rs:1262`). Expected: Resume starts a new holder and the panel switches to it. Pinned by Task 1 (thread file removed on exit; `is_ended` filter in `prepare`).
2. **App relaunch with mirrors still naming ended holders.** Mirrors persist for a day, so a relaunch would show yesterday's screen again. Expected: the screen is gone after relaunch. Pinned by Task 1 (`stop_ended` at launch) and Task 2 (`orphans`).
3. **A viewer left on a frozen screen after the Mac quits.** No event marks the lapse. Expected: the panel flips to "The screen is gone" within one watch ping (15 s) after `screenUntil` lapses. Pinned by Task 4 (`watch` publishes `workspace.sessions.changed` when it refuses a lapsed screen).
4. **Typing into an ended screen.** Expected: nothing reaches the pty and the terminal view is read-only. Pinned by Task 1 (holder ignores INPUT after exit, `lastInput` unchanged), Task 4 (`type_into` still refuses an ended row) and Task 6 (`admitted` false).
5. **Mixed versions during rollout** (old relay + new desktop, new relay + old desktop, old web). Expected: no frame is rejected and nothing regresses below S1. Pinned by Task 2 (fields only after the feature is listed) and Task 3 (`screens` defaults to `[]`, is not a device feature).

---

`routers/messaging.py` (`watch_local_session`, line 245) and `SessionFocus.tsx` need no change: the route calls `session_screen.watch`, and `SessionFocus` already fetches an ended session directly (lines 20-38).

---

### Task 1: The holder keeps its final screen (S5, Rust)

**Files:**
- Modify: `apps/desktop/src-tauri/src/holder/run.rs:278, 316-320, 331, 358, 377-378, 383, 432-451`
- Modify (add-only): `apps/desktop/src-tauri/src/holder/manager.rs` (append after `bind_thread`, line 284)
- Modify: `apps/desktop/src-tauri/src/local_sessions/resume.rs:266`
- Modify: `apps/desktop/src-tauri/src/lib.rs` (`.setup`, after `file_access::init`, ~line 1875)
- Test: `apps/desktop/src-tauri/tests/holder.rs:92-108` (replace), `apps/desktop/src-tauri/tests/cli.rs:210`

**Interfaces:**
- Produces: `holder::manager::is_ended(info: &HolderInfo) -> bool`; `holder::manager::stop_ended()`; HELLO facts gain `"ended": bool`.

- [ ] **Step 1: Write the failing test.** Replace `holder_exits_with_its_provider_and_removes_its_socket` (tests/holder.rs lines 92-108) with:

```rust
#[test]
fn holder_keeps_the_final_screen_after_its_provider_exits_and_refuses_input() {
    let dir = temp_dir();
    let socket = start(&dir, &["/bin/sh", "-c", "read x; printf final-screen; exit 0"]);
    std::fs::write(socket.with_extension("thread"), "11111111-1111-4111-8111-111111111111").unwrap();
    let mut c = attach(&socket, "interactive");
    c.write_all(&encode(INPUT, b"go\r")).unwrap();
    loop {
        let (kind, _) = read_frame(&mut c).expect("EXIT before timeout");
        if kind == EXIT {
            break;
        }
    }
    assert_eq!(last_input(&socket), "owner");
    // Still listed, and it says it ended.
    let info = manager::list_in(&dir).into_iter().next().expect("the holder stays after its provider");
    assert!(manager::is_ended(&info));
    assert!(!socket.with_extension("thread").exists(), "an ended holder never claims a Codex thread");
    // A new viewer gets the final screen.
    let mut w = UnixStream::connect(&socket).unwrap();
    w.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    w.write_all(&encode(HELLO, br#"{"role":"watch"}"#)).unwrap();
    let (kind, hello) = read_frame(&mut w).unwrap();
    assert_eq!(kind, HELLO);
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&hello).unwrap()["ended"], true);
    let (kind, snap) = read_frame(&mut w).unwrap();
    assert_eq!(kind, SNAPSHOT);
    assert!(split_offset(&snap).unwrap().1.windows(12).any(|x| x == b"final-screen"));
    // An interactive client is only a watcher now, and a teammate's keys go nowhere.
    let mut late = UnixStream::connect(&socket).unwrap();
    late.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    late.write_all(&encode(HELLO, br#"{"role":"interactive","cols":10,"rows":5}"#)).unwrap();
    assert_eq!(read_frame(&mut late).unwrap().0, HELLO);
    assert_eq!(read_frame(&mut late).unwrap().0, SNAPSHOT);
    assert_eq!(size_of(&socket), (120, 40), "a late window never resizes the final screen");
    let mut t = UnixStream::connect(&socket).unwrap();
    t.write_all(&encode(HELLO, br#"{"role":"type"}"#)).unwrap();
    t.write_all(&encode(INPUT, b"typed-after-exit")).unwrap();
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(last_input(&socket), "owner", "input after the end is refused");
    // ponytail: the holder thread outlives this test (in-process); `manager::stop` would signal the test runner.
}
```

`size_of` (tests/holder.rs:332, reads `cols`/`rows` from a HELLO) and `last_input` (line 298) already exist; `(120, 40)` is the unsized default (`run.rs:19-20`).

In `tests/cli.rs` line 210 replace

```rust
    eventually("the session to end", || !holder.exists());
```

with

```rust
    eventually("the session to end", || manager::hello(&holder).is_some_and(|h| manager::is_ended(&h)));
```

- [ ] **Step 2: Run to verify it fails.**
Run: `CARGO_TARGET_DIR=/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/target TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --test holder holder_keeps_the_final_screen`
Expected: compile error `cannot find function is_ended in module manager`.

- [ ] **Step 3: Add the manager functions (append to `manager.rs` after `bind_thread`; touch nothing else in the file).**

```rust
/// Whether this holder's provider exited and it only shows the final screen (spec 2026-10-07
/// §5.4). An older holder never says so; one that does not answer is not ended.
pub fn is_ended(info: &HolderInfo) -> bool {
    let Ok(mut conn) = UnixStream::connect(&info.socket) else { return false };
    if conn.set_read_timeout(Some(Duration::from_secs(2))).is_err()
        || conn.write_all(&encode(HELLO, br#"{"role":"origin"}"#)).is_err()
    {
        return false;
    }
    matches!(read_frame(&mut conn), Ok((HELLO, facts))
        if serde_json::from_slice::<serde_json::Value>(&facts).is_ok_and(|v| v["ended"] == true))
}

/// Close every holder that only shows an ended session's final screen. The app does this once at
/// launch: an ended screen lives only while the app that saw it end runs (spec §5.4).
pub fn stop_ended() {
    for holder in list() {
        if is_ended(&holder) {
            stop(holder.pid);
        }
    }
}
```

- [ ] **Step 4: Ended mode in `run.rs`.**

After line 278 (`let mut exited: Option<(String, u32, Instant)> = None;`) add:

```rust
    // Spec 2026-10-07 §5.4: the provider exited; the final screen stays for watchers, in memory only.
    let mut ended = false;
```

Lines 316-320 become:

```rust
                let joining = !ended && role["role"] == "interactive" && role["join"] == true && interactive.is_some();
                let (rows, cols) = model.size();
                let facts = serde_json::json!({"protocol": 1, "pid": std::process::id(),
                    "providerPid": provider_pid, "argv": argv, "cwd": cwd, "rows": rows, "cols": cols,
                    "watch": true, "type": true, "joined": joining, "origin": true, "lastInput": last_input,
                    "ended": ended});
```

Line 331 `} else if role["role"] == "interactive" {` becomes `} else if role["role"] == "interactive" && !ended {`.
Line 358 `} else if role["role"] == "watch" {` becomes `} else if role["role"] == "watch" || (ended && role["role"] == "interactive") {` (after the end an interactive window, the desktop panel included, only watches).

Lines 377-378 guard gains `!ended`:

```rust
            Event::Frame(id, kind @ (INPUT | OWNER_INPUT), payload)
                if !ended && (interactive == Some(id) || typists.contains(&id)) && !is_probe_reply(&payload) =>
```

Line 383: `Event::Frame(id, RESIZE, payload) if !ended && interactive == Some(id) && payload.len() == 4 => {`

Replace the `Event::Eof` arm body after the `exited.is_none()` check (lines 437-450) with:

```rust
                let (status, code) = exited.take().map(|(s, c, _)| (s, c)).unwrap_or_default();
                let frame = encode(EXIT, serde_json::json!({ "status": status, "code": code }).to_string().as_bytes());
                // Every client hears the end (the CLI exits with the code, the screen relay restarts
                // as a watcher of the final screen). The socket, model and log stay until the app
                // stops this holder (`manager::stop`): nothing is written anywhere.
                clients.retain(|_, c| c.send(frame.clone()));
                ended = true;
                interactive = None;
                typists.clear();
                remote.clear();
                joined.clear();
                // A resumed Codex thread must bind to its new holder, never to this one.
                let _ = std::fs::remove_file(socket.with_extension("thread"));
```

(The `return Ok(())` and the writer-drain loop go. `exited` is now `None`, so the loop waits on `rx.recv()` again; a second reader `Eof` hits the existing `if exited.is_none() { continue; }`.)

- [ ] **Step 5: Resume ignores ended holders.** `resume.rs:266` becomes:

```rust
    if crate::holder::manager::list().iter().any(|h| h.argv.iter().any(|a| a == &w.conversation_id) && !crate::holder::manager::is_ended(h)) {
```

- [ ] **Step 6: Close ended holders at launch.** In `lib.rs` `.setup(|app| {`, after the `file_access::init` block (~line 1875):

```rust
            // An ended session's final screen lives only while the app that saw it end runs (spec 2026-10-07 §5.4).
            std::thread::spawn(holder::manager::stop_ended);
```

- [ ] **Step 7: Run the tests.**
Run: `CARGO_TARGET_DIR=/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/target TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --test holder` then the same with `--test cli`, then `--lib holder`.
Expected: all PASS (`holder_delivers_a_provider_s_last_words_and_exit_code_before_exit` still sees EXIT with code 3).

- [ ] **Step 8: Commit.**

```bash
git add apps/desktop/src-tauri/src/holder/run.rs apps/desktop/src-tauri/src/holder/manager.rs apps/desktop/src-tauri/src/local_sessions/resume.rs apps/desktop/src-tauri/src/lib.rs apps/desktop/src-tauri/tests/holder.rs apps/desktop/src-tauri/tests/cli.rs
git commit -m "feat(desktop): an ended held session keeps its final screen while the app runs (S5)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: The desktop reports kept screens and terminal reasons (S5, S6, Rust)

**Files:**
- Create: `apps/desktop/src-tauri/src/local_sessions/session_states.rs`
- Create: `packages/shared/fixtures/local-session-frames/device-heartbeat-screens.json`
- Modify: `apps/desktop/src-tauri/src/local_sessions.rs` (module list lines 1-20; `impl NativeSession` lines 1009-1019)
- Modify: `apps/desktop/src-tauri/src/local_sessions/transport.rs` — six one-line seams, nothing else:
  1. line 1178 `super::screen::index(&self.mirrors);` → `super::session_states::index(&self.mirrors);`
  2. line 1346 the same replacement.
  3. after line 1273 (`let frames = device_heartbeats_with_controls(...)?;`) insert `let frames = super::session_states::with_screens(frames, &self.mirrors);`
  4. after line 1421 (`relay_reads_terminal = …"sessionTerminal"…;`) insert `super::session_states::read_features(&body["features"]);`
  5. line 763 `let frame = json!({"type":"local.session.ended",…` → `let mut frame = json!(…` (same text), and after its closing line 766 insert `super::session_states::add_screen(&mut frame, mirror);`
  6. inside `if terminal {` at line 2302-2304, after `entry["terminal"] = json!(m.native.terminal());` insert `super::session_states::add_reason(&mut entry, &m.native);`
  Why `transport.rs` cannot be avoided: the heartbeat, the end frame, the feature list and the index call all live there; each seam is one call into the new module.
- Test: in `session_states.rs`

**Interfaces:**
- Consumes: `manager::is_ended`, `manager::socket_dir`, `manager::list`, `manager::stop` (Task 1).
- Produces (wire): heartbeat frame `{"type":"local.device.heartbeat","sessions":[],"screens":[sessionId…≤24]}`; `local.session.ended` gains `"screen": true`; heartbeat session entry gains `"terminalReason"`. Relay features read: `endedScreens`, `terminalReason`.

- [ ] **Step 1: Golden fixture.** Create `packages/shared/fixtures/local-session-frames/device-heartbeat-screens.json`:

```json
{"type":"local.device.heartbeat","sessions":[],"screens":["aaaabbbb-cccc-4ddd-8eee-ffff00001111"]}
```

- [ ] **Step 2: Write the module with its failing tests.** Add `mod session_states;` after `pub(crate) mod screen;` (local_sessions.rs line 16). Create `local_sessions/session_states.rs`:

```rust
//! Session states beyond a running terminal (spec 2026-10-07 §5.1, §5.4): an ended held session's
//! final screen, which its holder keeps while this app runs, and why a running session has no
//! terminal. `transport.rs` calls in here at six one-line seams; `screen.rs` is untouched: a kept
//! ended screen is indexed as live, so a viewer's watch reaches its holder like a running one's.

use super::{Mirror, NativeSession};
use crate::holder::manager::{self, HolderInfo};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// The relay lists `endedScreens`: it reads `screens` heartbeats and `screen` on an end.
static READS_ENDED_SCREENS: AtomicBool = AtomicBool::new(false);
/// The relay lists `terminalReason`.
static READS_TERMINAL_REASON: AtomicBool = AtomicBool::new(false);
/// `DeviceHeartbeat.screens`' cap on the relay.
const SCREENS_MAX: usize = 24;
/// How often holders no mirror names are looked for.
const SWEEP_EVERY: Duration = Duration::from_secs(10);
static SWEPT: Mutex<Option<Instant>> = Mutex::new(None);

/// A registration ack's `features`. Per connection, like the other `relay_reads_*` flags: every
/// heartbeat follows a registration ack on the same connection.
pub(super) fn read_features(features: &Value) {
    let has = |name: &str| features.as_array().is_some_and(|f| f.iter().any(|x| x == name));
    READS_ENDED_SCREENS.store(has("endedScreens"), SeqCst);
    READS_TERMINAL_REASON.store(has("terminalReason"), SeqCst);
}

/// The holder still listens: its final screen is there. A connect only, never a wait on a reply.
fn holder_listens(id: &str) -> bool {
    UnixStream::connect(manager::socket_dir().join(format!("{id}.sock"))).is_ok()
}

/// An ended, registered held session whose holder still shows its final screen.
fn keeps_screen(m: &Mirror, listens: &impl Fn(&str) -> bool) -> bool {
    m.ended_at.is_some() && m.session_id.is_some() && m.native.holder.as_deref().is_some_and(|id| listens(id))
}

/// The ended screens this Mac still shows, newest end first, one frame's worth.
fn screens(mirrors: &BTreeMap<String, Mirror>, listens: &impl Fn(&str) -> bool) -> Vec<String> {
    let mut kept: Vec<&Mirror> = mirrors.values().filter(|m| keeps_screen(m, listens)).collect();
    kept.sort_by_key(|m| std::cmp::Reverse(m.ended_at));
    kept.into_iter().take(SCREENS_MAX).filter_map(|m| m.session_id.clone()).collect()
}

fn screens_frame(sids: &[String]) -> Value {
    json!({"type": "local.device.heartbeat", "sessions": [], "screens": sids})
}

/// The heartbeat's frames, plus one naming the kept ended screens for a relay that reads it.
pub(super) fn with_screens(mut frames: Vec<Value>, mirrors: &BTreeMap<String, Mirror>) -> Vec<Value> {
    if READS_ENDED_SCREENS.load(SeqCst) {
        let sids = screens(mirrors, &holder_listens);
        if !sids.is_empty() {
            frames.push(screens_frame(&sids));
        }
    }
    frames
}

/// `local.session.ended` says the final screen stays, so viewers keep it without waiting for a heartbeat.
pub(super) fn add_screen(frame: &mut Value, mirror: &Mirror) {
    if READS_ENDED_SCREENS.load(SeqCst) && keeps_screen(mirror, &holder_listens) {
        frame["screen"] = json!(true);
    }
}

/// A heartbeat entry's `terminalReason`, for a relay that reads it.
pub(super) fn add_reason(entry: &mut Value, native: &NativeSession) {
    if let Some(reason) = native.terminal_reason().filter(|_| READS_TERMINAL_REASON.load(SeqCst)) {
        entry["terminalReason"] = json!(reason);
    }
}

/// `screen::index` with every kept ended screen indexed as live; then holders no mirror names are closed.
pub(super) fn index(mirrors: &BTreeMap<String, Mirror>) {
    let shown = with_kept(mirrors, &holder_listens);
    super::screen::index(shown.as_ref().unwrap_or(mirrors));
    sweep(mirrors.values().filter_map(|m| m.native.holder.clone()).collect());
}

/// The mirrors the screen index should see; None when no ended screen is kept (nothing to copy).
// ponytail: clones the live mirrors once a second while a screen is kept; an index that took ids would not (screen.rs is S2's).
fn with_kept(mirrors: &BTreeMap<String, Mirror>, listens: &impl Fn(&str) -> bool) -> Option<BTreeMap<String, Mirror>> {
    if !mirrors.values().any(|m| keeps_screen(m, listens)) {
        return None;
    }
    Some(
        mirrors
            .iter()
            .filter(|(_, m)| m.ended_at.is_none() || keeps_screen(m, listens))
            .map(|(k, m)| (k.clone(), Mirror { ended_at: None, ..m.clone() }))
            .collect(),
    )
}

/// Off the relay loop, at most every SWEEP_EVERY: `list` and `is_ended` talk to every holder.
fn sweep(named: BTreeSet<String>) {
    if cfg!(test) {
        return; // never signal the developer's own holders
    }
    let Ok(mut last) = SWEPT.lock() else { return };
    if last.is_some_and(|at| at.elapsed() < SWEEP_EVERY) {
        return;
    }
    *last = Some(Instant::now());
    std::thread::spawn(move || {
        for pid in orphans(&manager::list(), &named, manager::is_ended) {
            manager::stop(pid);
        }
    });
}

/// Ended holders no mirror names: a resumed session's old holder once discovery names the new
/// one, or a session reaped after a day. A running holder nobody names yet (just started) never is.
fn orphans(holders: &[HolderInfo], named: &BTreeSet<String>, ended: impl Fn(&HolderInfo) -> bool) -> Vec<u32> {
    holders.iter().filter(|h| !named.contains(&h.id) && ended(h)).map(|h| h.pid).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::transport::tests::test_mirror;

    const SID: &str = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";
    const GOLDEN: &str =
        include_str!("../../../../../packages/shared/fixtures/local-session-frames/device-heartbeat-screens.json");

    fn ended(holder: &str, at: u64, sid: &str) -> Mirror {
        let mut m = test_mirror(&format!("thread-{holder}"), "gen-1");
        m.session_id = Some(sid.into());
        m.native.holder = Some(holder.into());
        m.ended_at = Some(at);
        m
    }

    #[test]
    fn an_ended_screen_is_kept_only_while_its_holder_listens_and_is_indexed_as_live() {
        let mut live = test_mirror("thread-live", "gen-1");
        live.session_id = Some("live".into());
        let mirrors = BTreeMap::from([
            ("a".to_string(), ended("ab12cd34", 5, SID)),
            ("b".to_string(), ended("dead0000", 9, "gone")),
            ("c".to_string(), live),
        ]);
        let listens = |id: &str| id == "ab12cd34";
        assert_eq!(screens(&mirrors, &listens), vec![SID.to_string()]);
        let shown = with_kept(&mirrors, &listens).unwrap();
        assert_eq!(shown.keys().cloned().collect::<Vec<_>>(), ["a", "c"]);
        assert!(shown.values().all(|m| m.ended_at.is_none()));
        assert!(with_kept(&mirrors, &|_: &str| false).is_none(), "nothing kept: the mirrors go in as they are");
    }

    #[test]
    fn screens_are_newest_first_and_one_frame_at_most() {
        let mirrors: BTreeMap<String, Mirror> =
            (0..30u64).map(|i| (format!("k{i}"), ended(&format!("{i:08}"), i, &format!("s{i}")))).collect();
        let sids = screens(&mirrors, &|_: &str| true);
        assert_eq!(sids.len(), 24);
        assert_eq!(sids[0], "s29");
    }

    #[test]
    fn the_screens_frame_is_the_golden_one() {
        assert_eq!(screens_frame(&[SID.into()]), serde_json::from_str::<Value>(GOLDEN).unwrap());
    }

    #[test]
    fn only_an_ended_holder_no_mirror_names_is_closed() {
        let h = |id: &str, pid| HolderInfo { id: id.into(), socket: format!("/x/{id}.sock").into(), pid, provider_pid: 2,
            argv: vec![], cwd: "/".into(), watch: true, type_input: true };
        let holders = [h("named000", 10), h("fresh000", 11), h("old00000", 12)];
        let named = BTreeSet::from(["named000".to_string()]);
        assert_eq!(orphans(&holders, &named, |x: &HolderInfo| x.id != "fresh000"), vec![12]);
    }

    #[test]
    fn a_running_session_without_a_terminal_says_why() {
        let mut m = test_mirror("thread-1", "gen-1"); // Codex, no holder, no job
        m.native.process.executable = "/opt/homebrew/bin/codex".into();
        assert_eq!(m.native.terminal_reason(), Some("no-daemon"));
        m.native.process.executable = "/Applications/ChatGPT.app/Contents/Resources/codex".into();
        assert_eq!(m.native.terminal_reason(), Some("unknown"), "the Codex app is S7's `app`");
        m.native.provider = "claude".into();
        assert_eq!(m.native.terminal_reason(), Some("plain"));
        m.native.job = Some("8ed71738".into());
        assert_eq!(m.native.terminal_reason(), None, "attach");
        m.native.job = None;
        m.native.holder = Some("ab12cd34".into());
        assert_eq!(m.native.terminal_reason(), None, "held");
    }

    #[test]
    fn the_new_fields_go_only_to_a_relay_that_lists_them() {
        let mut m = test_mirror("thread-1", "gen-1");
        m.native.provider = "claude".into();
        let mut entry = json!({});
        read_features(&json!(["sessionTerminal"]));
        add_reason(&mut entry, &m.native);
        assert!(entry.get("terminalReason").is_none());
        assert_eq!(with_screens(vec![json!({"type": "x"})], &BTreeMap::new()).len(), 1);
        let mut end = json!({"type": "local.session.ended"});
        add_screen(&mut end, &ended("ab12cd34", 5, SID));
        assert!(end.get("screen").is_none());
        read_features(&json!(["terminalReason", "endedScreens"]));
        add_reason(&mut entry, &m.native);
        assert_eq!(entry["terminalReason"], "plain");
        read_features(&Value::Null);
        assert!(!READS_ENDED_SCREENS.load(SeqCst) && !READS_TERMINAL_REASON.load(SeqCst));
    }
}
```

- [ ] **Step 3: Run to verify it fails.**
Run: `CARGO_TARGET_DIR=/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/target TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib session_states`
Expected: compile error `no method named terminal_reason`.

- [ ] **Step 4: `NativeSession::terminal_reason`.** In `local_sessions.rs`, inside `impl NativeSession` after `terminal()` (line 1018):

```rust
    /// Why a running session has no live terminal (spec 2026-10-07 §4 f, g); None when it has one.
    /// `plain`: Claude in a shell without "Share every session live" (← moves it to a background
    /// job). `no-daemon`: a Codex TUI off Codex's shared daemon (`codex --no-daemon`).
    fn terminal_reason(&self) -> Option<&'static str> {
        if self.terminal() != "none" {
            return None;
        }
        Some(match self.provider.as_str() {
            "claude" => "plain",
            // ponytail: codex::DESKTOP_RESOURCES's app, repeated; S7 replaces this arm with `app`.
            "codex" if self.process.executable.starts_with("/Applications/ChatGPT.app/") => "unknown",
            "codex" => "no-daemon",
            _ => "unknown",
        })
    }
```

- [ ] **Step 5: The six `transport.rs` seams** exactly as listed under **Files**. No other line in `transport.rs` changes.

- [ ] **Step 6: Run the tests.**
Run the Step 3 command, then the same with `--lib local_sessions::transport` and `--lib local_sessions::screen`.
Expected: all PASS (existing heartbeat tests see no new field: `read_features` was never called with the new names).

- [ ] **Step 7: Commit.**

```bash
git add apps/desktop/src-tauri/src/local_sessions/session_states.rs apps/desktop/src-tauri/src/local_sessions.rs apps/desktop/src-tauri/src/local_sessions/transport.rs packages/shared/fixtures/local-session-frames/device-heartbeat-screens.json
git commit -m "feat(desktop): report kept ended screens and why a session has no terminal (S5, S6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Backend contract and public projection (S5, S6)

**Files:**
- Modify: `apps/backend/core/services/local_sessions.py` (top: after `STATUSES`, line 27; `LocalSession` lines 261-263 and drop list 312-333; `session_terminal` line 590; `_session_public` line 654)
- Modify: `apps/backend/core/services/local_session_relay.py` (`HeartbeatSession` 88-91; `FEATURES` 95-106; `DeviceHeartbeat` 119, 132-133; `Ended` 136-141; `handle_message` 622-625; `_device_heartbeat` 747-748, 782; `_ended` 833)
- Modify: `apps/backend/core/services/session_screen.py` (add `keep_screens` after `ack_with_watching`, line 147)
- Test: create `apps/backend/tests/unit/services/test_session_states.py`

**Interfaces:**
- Consumes (wire, from Task 2): `screens`, `screen`, `terminalReason`.
- Produces: `local_sessions.TerminalReason`; `local_sessions.screen_available(row: dict, now: int | None = None) -> bool`; `LocalSession.terminal_reason`, `LocalSession.screen_available` (JSON `terminalReason`, `screenAvailable`); `session_screen.keep_screens(owner, human, device, cid, sids: list[str]) -> None`; META fields `screenUntil`, `screenConnectionId`, `terminalReason`.

- [ ] **Step 1: Write the failing tests.** Create `tests/unit/services/test_session_states.py`:

```python
"""Ended screens and terminal reasons (spec 2026-10-07 §5.1, §5.4): what the Mac reports and
what every reader is shown."""

# ruff: noqa: F811 -- fixtures imported by name are re-requested as test arguments

import time

import pytest

from core.repositories import local_sessions_repo as repo
from core.services import local_session_relay as relay_module, local_sessions as service
from core.services.messaging import ApiProblem
from tests.unit.services.test_local_session_relay import (  # noqa: F401 -- fixtures
    _own_spawned_tasks,
    connect,
    db,
    fixture,
    heartbeat,
    relay,
    start,
    transport,
)
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER
from tests.unit.services.test_session_screen import (  # noqa: F401 -- autouse fixtures
    _nudges_reach_the_test_mac,
    _teammates_are_members,
    ack,
)


async def end(transport, sid, **extra):
    body = {**fixture("session-ended"), "sessionId": sid, "generation": "generation-1", "endedAt": int(time.time()), **extra}
    return await relay(transport).handle_message("conn-1", body)


def screens(*sids):
    return {**fixture("device-heartbeat-screens"), "screens": list(sids)}


async def public(sid, viewer=BOB):
    return service._session_public(await repo.get_session(OWNER, sid), viewer.user_id)


def set_meta(db, sid, **fields):
    db[0].update_item(
        Key={"pk": repo.session_pk(OWNER, sid), "sk": "META"},
        UpdateExpression="SET " + ", ".join(f"{k} = :{k}" for k in fields),
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


def test_the_relay_lists_both_features_and_screens_is_no_device_feature():
    assert {"endedScreens", "terminalReason"} <= set(relay_module.FEATURES)
    frame = relay_module.DeviceHeartbeat.model_validate(fixture("device-heartbeat-screens"))
    assert frame.device_features() == []
    assert relay_module.DeviceHeartbeat.model_validate(fixture("device-heartbeat")).screens == []


@pytest.mark.asyncio
async def test_the_mac_names_why_a_running_session_has_no_terminal(transport):
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="none", terminalReason="no-daemon"))
    seen = await public(sid)
    assert (seen.terminal, seen.terminal_reason) == (None, "no-daemon")
    assert seen.model_dump(by_alias=True, mode="json")["terminalReason"] == "no-daemon"
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="held"))
    assert "terminalReason" not in await repo.get_session(OWNER, sid)
    assert (await public(sid)).terminal_reason is None
    with pytest.raises(ApiProblem):
        await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="none", terminalReason="vscode"))


@pytest.mark.asyncio
async def test_an_ended_held_session_keeps_its_terminal_while_the_mac_keeps_its_screen(transport, db):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    seen = await public(sid)
    assert (seen.status, seen.terminal, seen.screen_available) == ("ended", "held", True)
    assert seen.terminal_reason is None
    set_meta(db, sid, screenUntil=1)
    seen = await public(sid)
    assert (seen.terminal, seen.screen_available) == (None, None)
    assert "screenAvailable" not in seen.model_dump(by_alias=True, mode="json", exclude_none=True)
    await relay(transport).handle_message("conn-1", screens(sid))
    row = await repo.get_session(OWNER, sid)
    assert row["screenConnectionId"] == "conn-1" and int(row["screenUntil"]) > time.time()
    assert (await public(sid)).screen_available is True


@pytest.mark.asyncio
async def test_an_end_without_a_kept_screen_shows_no_terminal(transport):
    sid = await start(transport)
    await end(transport, sid)
    seen = await public(sid)
    assert (seen.status, seen.terminal, seen.screen_available) == ("ended", None, None)


@pytest.mark.asyncio
async def test_screens_are_kept_only_for_this_macs_ended_held_sessions(transport):
    sid = await start(transport)
    await relay(transport).handle_message("conn-1", screens(sid))
    assert "screenUntil" not in await repo.get_session(OWNER, sid), "a running session is not an ended screen"
    await relay(transport).handle_message("conn-1", heartbeat(sid, terminal="attach"))
    await end(transport, sid, screen=True)
    await relay(transport).handle_message("conn-1", screens(sid))
    assert "screenUntil" not in await repo.get_session(OWNER, sid), "an attach session's screen closes with it"
```

- [ ] **Step 2: Run to verify it fails.**
Run (from `apps/backend`): `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_session_states.py -q --no-cov`
Expected: FAIL (`assert {...} <= set(FEATURES)`; `INVALID_REQUEST` on `terminalReason`).

- [ ] **Step 3: `local_sessions.py`.**

After `STATUSES` (line 27) add:

```python
# Why a running session has no live terminal, as its Mac says (spec 2026-10-07 §5.1).
TerminalReason = Literal["plain", "no-daemon", "app", "sdk", "unknown"]
```

In `LocalSession`, after `terminal: Literal["held", "attach"] | None = None` (line 263):

```python
    # Why a running session has no live terminal (spec 2026-10-07 §4 f, g); absent when it has one.
    terminal_reason: TerminalReason | None = None
    # An ended held session whose final screen its owner's Mac still shows (spec §5.4); `terminal`
    # stays set while it is. Absent otherwise.
    screen_available: bool | None = None
```

Add `"terminal_reason",` and `"screen_available",` after `"terminal",` in the `_drop_none` tuple (line 317).

After `session_terminal` (line 596) add:

```python
def screen_available(row: dict, now: int | None = None) -> bool:
    """An ended held session whose holder still shows its final screen (spec 2026-10-07 §5.4). The
    Mac says so on the end and renews `screenUntil` with every heartbeat while it lasts; it lapses
    when the app quits, relaunches or closes that holder."""
    if row.get("endedAt") is None or session_terminal(row) != "held":
        return False
    return int(row.get("screenUntil") or 0) > (int(time.time()) if now is None else now)
```

Replace line 654 (`public["terminal"] = None if public["status"] in ("ended", "offline") else session_terminal(row)`) with:

```python
    screen = public["status"] == "ended" and screen_available(row)
    public["terminal"] = session_terminal(row) if public["status"] not in ("ended", "offline") or screen else None
    if screen:
        public["screenAvailable"] = True
    reason = public.pop("terminalReason", None)
    if public["status"] not in ("ended", "offline") and public["terminal"] is None and reason:
        public["terminalReason"] = reason
```

- [ ] **Step 4: `local_session_relay.py`.**

`HeartbeatSession`, after `terminal: …` (line 91):

```python
    # Why `terminal` is "none" (spec 2026-10-07 §5.1). Sent only once FEATURES lists terminalReason.
    terminal_reason: sessions.TerminalReason | None = None
```

`FEATURES`: append `"endedScreens",` and `"terminalReason",` after `"bandOwnerOnly",`.

`DeviceHeartbeat`, after `sessions: …` (line 119):

```python
    # Ended held sessions whose holder still shows the final screen (spec §5.4). Sent only once
    # FEATURES lists endedScreens, in a frame of its own with no sessions.
    screens: Annotated[list[uuid.UUID], Field(max_length=24)] = []
```

and `device_features` (line 133) excludes it: `exclude={"type", "sessions", "screens"}`.

`Ended`, after `ended_at` (line 141):

```python
    # The holder keeps the final screen (spec §5.4). Sent only once FEATURES lists endedScreens.
    screen: bool = False
```

`handle_message`, in the `DeviceHeartbeat` branch before `response = await session_screen.ack_with_watching(` (line 625):

```python
                    # First, so this frame's own ack already lists the screens it keeps.
                    await session_screen.keep_screens(owner, human, device, cid, [str(s) for s in message.screens])
```

`_device_heartbeat`, lines 747-748 become:

```python
                if item.terminal is not None:
                    updated["terminal"] = item.terminal
                    if item.terminal == "none" and item.terminal_reason is not None:
                        updated["terminalReason"] = item.terminal_reason
                    else:
                        updated.pop("terminalReason", None)
```

and line 782:

```python
                terminal_changed = (row.get("terminal"), row.get("terminalReason")) != (updated.get("terminal"), updated.get("terminalReason"))
```

`_ended`, after `updated = _revised(row, status="ended", …)` (line 833):

```python
        if message.screen and sessions.session_terminal(row) == "held":
            # The holder keeps the final screen; the Mac's `screens` heartbeats renew this lease.
            updated["screenUntil"] = int(time.time()) + LIVENESS_SECONDS
            updated["screenConnectionId"] = cid
        else:
            updated.pop("screenUntil", None)
```

- [ ] **Step 5: `session_screen.keep_screens`.** Add after `ack_with_watching`:

```python
async def keep_screens(owner: str, human: str, device: str, cid: str, sids: list[str]) -> None:
    """The Mac's ended held sessions whose holder still shows the final screen (spec 2026-10-07
    §5.4): each stays watchable for one more lease. Only this Mac's own; never raises (a lost
    renewal lapses the screen 30 s later, nothing worse)."""
    from core.services.local_session_relay import LIVENESS_SECONDS  # it imports this module

    now = int(_clock())
    for sid in sids:
        try:
            row = await repo.get_session(owner, sid)
            if (
                row is None
                or row.get("humanUserId") != human
                or row.get("deviceId") != device
                or row.get("endedAt") is None
                or sessions.session_terminal(row) != "held"
            ):
                continue
            # ponytail: a write per kept screen per heartbeat (10 s); skip it while the lease is fresh if that ever shows.
            await store.run_in_thread(
                store._table().update_item,
                Key={"pk": repo.session_pk(owner, sid), "sk": "META"},
                UpdateExpression="SET screenUntil = :u, screenConnectionId = :c",
                ConditionExpression="attribute_exists(endedAt) AND deviceId = :d",
                ExpressionAttributeValues={":u": now + LIVENESS_SECONDS, ":c": cid, ":d": device},
            )
        except Exception as exc:  # noqa: BLE001 -- see docstring
            logger.info("ended screen not kept error=%s", type(exc).__name__)
```

- [ ] **Step 6: Run the tests.**
Run: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_session_states.py tests/unit/services/test_local_session_relay.py tests/unit/services/test_local_sessions.py tests/unit/services/test_session_screen.py -q --no-cov`
Expected: all PASS.

- [ ] **Step 7: Commit.**

```bash
git add apps/backend/core/services/local_sessions.py apps/backend/core/services/local_session_relay.py apps/backend/core/services/session_screen.py apps/backend/tests/unit/services/test_session_states.py
git commit -m "feat(sessions): ended screens and terminal reasons on the relay contract (S5, S6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: An ended screen can be watched (S5, backend)

**Files:**
- Modify: `apps/backend/core/services/session_screen.py` (`watch` 63-73; `_nudge` 111-123; `ack_with_watching` 137; `forward` 210-224)
- Test: `apps/backend/tests/unit/services/test_session_states.py` (append)

**Interfaces:**
- Consumes: `local_sessions.screen_available` (Task 3), META `screenConnectionId`.
- Produces: `watch` admits an ended row while `screen_available`; when it refuses a lapsed one it publishes `workspace.sessions.changed` for the owner's sessions.

- [ ] **Step 1: Write the failing tests.** Append to `test_session_states.py`:

```python
from core.services import live_events, session_screen  # noqa: E402
from tests.unit.services.test_session_screen import _published, frame  # noqa: E402


@pytest.mark.asyncio
async def test_a_kept_final_screen_is_watched_streamed_and_never_typed_into(transport):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    await session_screen.watch(BOB, sid)
    acked = await relay(transport).handle_message("conn-1", screens(sid))
    assert [w["sessionId"] for w in acked["watching"]] == [sid]
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    bob = await live_events.get_live_event_publisher().user_channel(OWNER, BOB.user_id)
    assert [(c, e["type"]) for c, e in await _published(transport)] == [(bob, "workspace.session.screen")]
    with pytest.raises(ApiProblem) as typed:
        await session_screen.type_into(
            ALICE, sid, session_screen.SessionInput.model_validate({"data": "YQ==", "epoch": 1})
        )
    assert typed.value.error.code == "LOCAL_SESSION_OFFLINE"


@pytest.mark.asyncio
async def test_a_screen_frame_for_an_ended_session_needs_its_kept_screen_and_connection(transport, db):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    await session_screen.watch(BOB, sid)
    set_meta(db, sid, screenConnectionId="conn-2")
    transport[4].clear()
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert await _published(transport) == [], "another connection's frame"
    set_meta(db, sid, screenConnectionId="conn-1", screenUntil=1)
    await session_screen.forward(relay(transport), "conn-1", frame(sid))
    assert await _published(transport) == [], "a lapsed screen"


@pytest.mark.asyncio
async def test_a_lapsed_final_screen_refuses_watching_and_tells_the_open_panels(transport, db, monkeypatch):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    set_meta(db, sid, screenUntil=1)
    told = []

    async def hint(owner, human, **_):
        told.append((owner, human))

    monkeypatch.setattr(relay_module, "publish_resources_changed", hint)
    with pytest.raises(ApiProblem) as refused:
        await session_screen.watch(BOB, sid)
    assert refused.value.error.code == "LOCAL_SESSION_OFFLINE"
    assert told == [(OWNER, ALICE.user_id)]
    assert (await relay(transport).handle_message("conn-1", screens()))["watching"] == []


@pytest.mark.asyncio
async def test_a_new_viewer_of_a_kept_screen_nudges_the_connection_that_keeps_it(transport, db):
    sid = await start(transport)
    await ack(transport, sid)
    await end(transport, sid, screen=True)
    set_meta(db, sid, connectionId="conn-old")
    transport[3].clear()
    await session_screen.watch(BOB, sid)
    assert [(c, b["type"]) for c, b in transport[3]] == [("conn-1", "local.session.watch")]
```

`screens()` with no ids is a valid empty frame (`screens: []`). `transport[3]` holds the `(connectionId, body)` pairs sent to Macs, as `test_session_screen.py:211` asserts.

- [ ] **Step 2: Run to verify it fails.**
Run: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_session_states.py -q --no-cov`
Expected: the four new tests FAIL with `LOCAL_SESSION_OFFLINE` from `watch`.

- [ ] **Step 3: `watch`.** Replace lines 68-73 (from `now = int(_clock())` through the `LOCAL_SESSION_UNSUPPORTED` raise) with:

```python
    now = int(_clock())
    kept = sessions.screen_available(row, now)
    if row.get("unshared") or (not kept and (row.get("endedAt") is not None or int(row.get("onlineUntil", 0)) <= now)):
        if row.get("endedAt") is not None and row.get("screenUntil") is not None:
            # The final screen just went away: open panels refetch now, not at their next hint.
            from core.services import local_session_relay  # it imports this module

            await local_session_relay.publish_resources_changed(row["ownerId"], row["humanUserId"])
        raise sessions.ApiProblem("LOCAL_SESSION_OFFLINE", "This session is not running.")
    if sessions.session_terminal(row) is None:
        raise sessions.ApiProblem(
            "LOCAL_SESSION_UNSUPPORTED", "This session has no live terminal. Its transcript is shown instead."
        )
```

- [ ] **Step 4: `_nudge`** sends to the connection that keeps an ended screen. In `_nudge`, replace `row["connectionId"]` in `send_message_async(row["connectionId"], envelope)` with:

```python
cid = row.get("screenConnectionId") if row.get("endedAt") is not None else row["connectionId"]
```

declared on the line before the `try:`, and pass `cid`.

- [ ] **Step 5: `ack_with_watching`** line 137 becomes:

```python
            if meta is None or meta.get("unshared") or (meta.get("endedAt") is not None and not sessions.screen_available(meta, now)):
```

- [ ] **Step 6: `forward`.** Replace the row check and the following `now = _clock()` (lines 210-221) with:

```python
        row = await repo.get_session(owner, sid)
        now = _clock()
        ended = row is not None and row.get("endedAt") is not None
        if (
            row is None
            or row.get("humanUserId") != connection["user_id"]
            or row.get("deviceId") != connection["device_id"]
            or row.get("generation") != frame.generation
            or row.get("unshared")
            or (
                not (sessions.screen_available(row, int(now)) and row.get("screenConnectionId") == cid)
                if ended
                else row.get("connectionId") != cid
            )
        ):
            return
```

- [ ] **Step 7: Run the tests.**
Run: `/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/backend/.venv/bin/python -m pytest tests/unit/services/test_session_states.py tests/unit/services/test_session_screen.py tests/unit/services/test_session_input.py tests/unit/services/test_session_presence.py -q --no-cov`
Expected: all PASS (`test_an_ended_session_cannot_be_watched` and `test_no_nudge_for_an_unshared_or_ended_session` still pass: those rows have no `screenUntil`).

- [ ] **Step 8: Commit.**

```bash
git add apps/backend/core/services/session_screen.py apps/backend/tests/unit/services/test_session_states.py
git commit -m "feat(sessions): watch an ended session's kept final screen (S5)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Shared type and the reason helper

**Files:**
- Modify: `packages/shared/src/messaging/types.ts:384-387`
- Create: `packages/shared/src/messaging/sessionStates.ts`, `packages/shared/src/messaging/sessionStates.test.ts`
- Modify: `packages/shared/src/index.ts` (after line 221)

**Interfaces:**
- Produces: `LocalSession.terminalReason?`, `LocalSession.screenAvailable?`; `terminalStateReason(session: LocalSession): "plain" | "no-daemon" | null`.

- [ ] **Step 1: Write the failing test** `sessionStates.test.ts`:

```ts
import { expect, it } from "vitest";
import { terminalStateReason } from "./sessionStates";
import type { LocalSession } from "./types";

const base: LocalSession = {
  sessionId: "s", deviceId: "mac", humanUserId: "alice", provider: "claude", generation: "g1", status: "idle",
  providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false },
  capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null,
};

it("names why a running session has no terminal, and nothing otherwise", () => {
  expect(terminalStateReason(base)).toBe("plain"); // an older Mac sends no reason: a plain Claude is `plain`
  expect(terminalStateReason({ ...base, provider: "codex" })).toBeNull();
  expect(terminalStateReason({ ...base, provider: "codex", terminalReason: "no-daemon" })).toBe("no-daemon");
  expect(terminalStateReason({ ...base, terminalReason: "unknown" })).toBeNull();
  expect(terminalStateReason({ ...base, terminal: "held", terminalReason: "plain" })).toBeNull();
  expect(terminalStateReason({ ...base, status: "ended" })).toBeNull();
  expect(terminalStateReason({ ...base, status: "offline" })).toBeNull();
});
```

- [ ] **Step 2: Run to verify it fails.**
Run (from `packages/shared`): `pnpm vitest run src/messaging/sessionStates.test.ts`
Expected: FAIL, cannot resolve `./sessionStates`.

- [ ] **Step 3: Implement.** In `types.ts` after `terminal?: "held" | "attach";` (line 387):

```ts
  /** Why a running session has no live terminal, as its Mac reports it (spec 2026-10-07 §5.1). */
  terminalReason?: "plain" | "no-daemon" | "app" | "sdk" | "unknown";
  /** An ended held session whose final screen its owner's Mac still shows; `terminal` stays set while it is. */
  screenAvailable?: boolean;
```

Create `sessionStates.ts`:

```ts
import type { LocalSession } from "./types";

/** Which explicit state a running session without a terminal shows (spec 2026-10-07 §4 f, g).
 *  A Mac older than `terminalReason` sends none: a plain Claude still reads as `plain`. */
export function terminalStateReason(session: LocalSession): "plain" | "no-daemon" | null {
  if (session.terminal || session.status === "ended" || session.status === "offline") return null;
  if (session.terminalReason === "plain" || session.terminalReason === "no-daemon") return session.terminalReason;
  return !session.terminalReason && session.provider === "claude" ? "plain" : null;
}
```

In `index.ts` after line 221: `export { terminalStateReason } from "./messaging/sessionStates";`

- [ ] **Step 4: Run.**
Run: `pnpm vitest run src/messaging/sessionStates.test.ts` and `pnpm exec tsc --noEmit` (from `packages/shared`).
Expected: PASS, no type errors.

- [ ] **Step 5: Commit.**

```bash
git add packages/shared/src/messaging/types.ts packages/shared/src/messaging/sessionStates.ts packages/shared/src/messaging/sessionStates.test.ts packages/shared/src/index.ts
git commit -m "feat(shared): screenAvailable, terminalReason and the state reason helper (S5, S6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Web state panels (S5, S6)

**Files:**
- Modify: `apps/frontend/src/components/messaging/LocalSessionPanel.tsx:7-11 (import), 72-76, 172-188, 242-245`
- Modify: `apps/frontend/src/components/messaging/messaging.css` (append at end)
- Test: `apps/frontend/src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx` (append)

**Interfaces:**
- Consumes: `terminalStateReason`, `LocalSession.screenAvailable` (Task 5); `canPickUp` (`SessionPickup.tsx:12`).

- [ ] **Step 1: Write the failing tests** (append):

```tsx
it("keeps an ended held session's final screen, read-only, under an Ended line with Resume", async () => {
  const ended: LocalSession = { ...session, status: "ended", screenAvailable: true, latestEventSeq: 4, resumable: true, endedAt: new Date(Date.now() - 120_000).toISOString() };
  render(<LocalSessionPanel session={ended} ownerName="Alice" own client={{ ...client, resumeLocalSession: vi.fn() } as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  await screen.findByRole("region", { name: "Live terminal (view only)" });
  expect(typing.admitted).toBe(false);
  expect(document.querySelector(".session-ended-line")!.textContent).toMatch(/^Ended · /);
  expect(screen.queryByText("The screen is gone: this session's terminal closed.")).toBeNull();
  expect(await screen.findByRole("button", { name: "Resume" })).toBeTruthy();
});

it("says the screen is gone once the Mac no longer shows it", () => {
  render(<LocalSessionPanel session={{ ...session, status: "ended", latestEventSeq: 4 }} ownerName="Alice" own={false} client={client as never} revision={0} onAccessLost={() => {}} onEvent={() => () => {}} />);
  expect(screen.getByText("The screen is gone: this session's terminal closed.")).toBeTruthy();
  expect(screen.queryByRole("region", { name: /Live terminal/ })).toBeNull();
});

it("offers Pick up and a disabled Resume while the owner's Mac is offline", () => {
  const offline: LocalSession = { ...session, status: "offline", pickup: { allowed: true, state: "ready", hasCode: false } };
  render(<LocalSessionPanel session={offline} ownerName="Alice" own={false} client={{ ...client, requestLocalSessionPickup: vi.fn() } as never} revision={0} onAccessLost={() => {}} />);
  expect(screen.getByText("Alice's Mac is offline")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
  expect(screen.getByText("Resume works once Alice's Mac is back. Pick up continues it on your Mac now.")).toBeTruthy();
  expect(screen.getByRole("button", { name: /Pick up/ })).toBeTruthy();
});

it("tells how a no-daemon Codex session gets its screen, and says nothing for a reason it cannot act on", () => {
  const props = { ownerName: "Alice", client: client as never, revision: 0, onAccessLost: () => {}, onEvent: () => () => {} };
  const codex: LocalSession = { ...session, provider: "codex", terminal: undefined, terminalReason: "no-daemon" };
  const line = (text: string) => screen.getByText((_, el) => el?.tagName === "P" && el.textContent === text);
  const mine = render(<LocalSessionPanel {...props} own session={codex} />);
  expect(line("Start it again with lightbulb codex to share its screen.")).toBeTruthy();
  mine.unmount();
  const theirs = render(<LocalSessionPanel {...props} own={false} session={codex} />);
  expect(line("Ask Alice to start it again with lightbulb codex.")).toBeTruthy();
  expect(screen.queryByText(/press ←/i)).toBeNull();
  theirs.unmount();
  render(<LocalSessionPanel {...props} own={false} session={{ ...session, terminal: undefined, terminalReason: "unknown" }} />);
  expect(screen.getByText("This session was not started through Lightbulb, so its screen is not here.")).toBeTruthy();
  expect(screen.queryByText(/press ←/i)).toBeNull();
});
```

- [ ] **Step 2: Run to verify it fails.**
Run (from `apps/frontend`): `pnpm vitest run src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx`
Expected: the four new tests FAIL; the rest PASS.

- [ ] **Step 3: Implement.** Add `terminalStateReason` to the `@lightbulb/shared` import (lines 7-11).

Lines 73-76 become:

```tsx
  const running = session.status !== "ended" && session.status !== "offline";
  // An ended held session keeps its final screen while the owner's app runs (spec 2026-10-07 §5.4).
  const endedScreen = session.status === "ended" && session.screenAvailable === true && hasTerminal;
  // An offline session still probes this Mac's holder: "offline" is the relay's view (sharing off, a reconnect), not the Mac's.
  const local = useLocalTerminal(session.status === "ended" || (running && !hasTerminal) ? null : { sessionId: session.sessionId }, slot, details === null);
  const watchRemote = local === "none" && hasTerminal && (running || endedScreen) && !!onEvent && !!client.watchLocalSession;
```

(The owner's own desktop panel is not used for an ended screen: `holder/panel.rs` closes its page on EXIT, so the owner watches it through the relay like everyone else.)

Lines 172-188 become:

```tsx
  const endedEmpty = session.provider === "claude" && session.latestEventSeq === 0 && !session.controlUnavailableReason;
  const endedAt = session.endedAt ? <time dateTime={session.endedAt} title={new Date(session.endedAt).toLocaleString()}>{timeAgo(session.endedAt)}</time> : null;
  const endedPanel = <div className="session-state-panel" role="status">
    <p className="session-state-title">{endedAt ? <>Ended {endedAt}</> : "Ended"}</p>
    <p>{endedEmpty ? "This session ended before anything was sent." : "The screen is gone: this session's terminal closed."}</p>
  </div>;
  const endedLine = <p className="session-ended-line" role="status">{endedAt ? <>Ended · {endedAt}</> : "Ended"}</p>;
  const macName = own ? "your" : `${ownerName}'s`;
  const offlinePanel = <div className="session-state-panel" role="status">
    <p className="session-state-title">{own ? "Your Mac is offline" : `${ownerName}'s Mac is offline`}</p>
    {session.lastEventAt && <p>Last seen {timeAgo(session.lastEventAt)}</p>}
    <Button type="button" variant="outline" size="sm" disabled>Resume</Button>
    <p>{`Resume works once ${macName} Mac is back.`}{!own && canPickUp(session) ? " Pick up continues it on your Mac now." : ""}</p>
  </div>;
  const reason = terminalStateReason(session);
  const noticePanel = <div className="session-state-panel" role="status">
    <p className="session-state-title">This session was not started through Lightbulb, so its screen is not here.</p>
    {reason === "plain" && (own
      ? <p>Press ← in its terminal to share this one live, or turn on Share every session live in {isDesktopApp()
        ? <Link className="underline underline-offset-4" href="/settings?panel=desktop#share-every-session-live">Settings</Link>
        : "Settings"} to make every new session live.</p>
      : <p>Ask {ownerName} to press ← in their terminal.</p>)}
    {reason === "no-daemon" && (own
      ? <p>Start it again with <code>lightbulb codex</code> to share its screen.</p>
      : <p>Ask {ownerName} to start it again with <code>lightbulb codex</code>.</p>)}
  </div>;
```

(The `Your Mac` offline line reads "Resume works once your Mac is back.")

Lines 242-245 become:

```tsx
      {session.status === "ended" ? (endedScreen ? endedLine : endedPanel) : session.status === "offline" ? offlinePanel : notStarted ? noticePanel : null}
      {watchRemote && <SessionTerminalView session={session} onEvent={onEvent!} watch={client.watchLocalSession!} onLive={setScreenLive} onCanType={setTermCanType}
        admitted={running && control && session.shared !== false && !(session.inputPaused && !own)}
        input={client.sendLocalSessionInput ? (bytes, epoch) => client.sendLocalSessionInput!(session.sessionId, toBase64(bytes), epoch) : undefined} />}
```

Append to `messaging.css`:

```css
/* Ended · <time> above an ended session's final screen (spec 2026-10-07 §5.4). */
.session-ended-line { flex: none; margin: 0; padding: 6px 12px; font-size: 13px; color: var(--fg-mute); }
```

- [ ] **Step 4: Run.**
Run: `pnpm vitest run src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx src/components/messaging/__tests__/SessionResume.test.tsx src/components/messaging/__tests__/SessionPickup.test.tsx`, then `pnpm exec tsc --noEmit`, then `pnpm lint`.
Expected: all PASS; the S1 tests ("never labels the status line…", "gives a plain Codex session no hint…") still pass.

- [ ] **Step 5: Commit.**

```bash
git add apps/frontend/src/components/messaging/LocalSessionPanel.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx
git commit -m "feat(sessions): ended final screen, offline Resume and Pick up, no-daemon line on the web (S5, S6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Hosting by default (S4)

**Files:**
- Modify: `apps/frontend/src/components/desktop/DesktopPermissionsGate.tsx:52-53`
- Modify: `apps/frontend/src/components/desktop/LocalSessions.tsx:76, 104`
- Modify: `apps/frontend/src/components/desktop/ExplicitSharingNotice.tsx:48`
- Modify: `apps/desktop/src-tauri/src/cli.rs:100-109` (and its test at 696-724)
- Test: `apps/frontend/src/components/desktop/__tests__/LocalSessions.test.tsx:271-290`

**Interfaces:**
- Produces: `SHARE_LIVE_FILES` (string) exported from `LocalSessions.tsx`.

- [ ] **Step 1: Write the failing tests.** In `LocalSessions.test.tsx` replace the two tests at lines 271-290 with:

```tsx
it("turns Share every session live on at pairing unless the person turns it off", async () => {
  invoke.mockImplementation(async (command) => command === "local_sessions_set" ? 1 : { enrollment:null, pairedWorkspaceId:"org", status:"paused" });
  render(<DesktopPermissionsGate />);
  const live = await screen.findByRole("button", {name:/Share every session live/});
  expect(live).toHaveAttribute("aria-pressed", "true");
  expect(live).toHaveTextContent("~/.zshrc, ~/.bashrc and your bash login file");
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("shell_integration_set", {enabled:true}));
});
it("records that pairing made the Share every session live offer, also when it was turned off", async () => {
  localStorage.removeItem("lightbulb.desktop.liveSharingOffer");
  invoke.mockImplementation(async (command) => command === "local_sessions_set" ? 1 : { enrollment:null, pairedWorkspaceId:"org", status:"paused" });
  render(<DesktopPermissionsGate />);
  await userEvent.click(await screen.findByRole("button", {name:/Share every session live/}));
  await userEvent.click(screen.getByRole("button", {name:"Continue"}));
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(invoke.mock.calls.some(([command]) => command === "shell_integration_set")).toBe(false);
  // The once-per-Mac notice for Macs paired before the switch existed must not ask again.
  expect(localStorage.getItem("lightbulb.desktop.liveSharingOffer")).toBe("1");
});
```

In `cli.rs` `cli_refuses_only_the_forms_with_no_terminal_to_share` (line 701) extend the claude refusals:

```rust
        for no in [&["-p", "hi"][..], &["--print", "hi"], &["-cp", "hi"], &["--print=json"], &["--bg"], &["--bg", "fix it"]] {
```

and add after line 719:

```rust
        assert_eq!(refusal("claude", &words(&["--", "--bg"])), None, "a prompt that says --bg is the prompt");
```

- [ ] **Step 2: Run to verify it fails.**
Run (from `apps/frontend`): `pnpm vitest run src/components/desktop/__tests__/LocalSessions.test.tsx` — expected FAIL (`aria-pressed` is `false`).
Run: `CARGO_TARGET_DIR=/Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/apps/desktop/src-tauri/target TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib cli::tests::cli_refuses_only` — expected FAIL on `["--bg"]`.

- [ ] **Step 3: Implement.**

`DesktopPermissionsGate.tsx` lines 52-53:

```tsx
  // On by default (owner ruling 2026-10-07): the item says which files it edits, and one click turns it off.
  const [live, setLive] = useState(true);
```

`LocalSessions.tsx` line 76:

```tsx
export const SHARE_LIVE_FILES = "~/.zshrc, ~/.bashrc and your bash login file (~/.bash_profile, ~/.bash_login or ~/.profile, whichever you have)";
export const SHARE_LIVE_DESCRIPTION = `Plain claude and codex in a new terminal tab start through Lightbulb, so their live terminal shows here too. Adds a marked block to ${SHARE_LIVE_FILES}; turning it off removes it.`;
```

`LocalSessions.tsx` line 104:

```tsx
    <p className="settings-row-note">Adds a marked block to {SHARE_LIVE_FILES}, and removes it when you turn this off. Open a new terminal tab after you change it.</p>
```

`ExplicitSharingNotice.tsx` line 48 (this file repeats the copy on purpose, see its line 46): replace `It adds a marked block to ~/.zshrc and ~/.bashrc.` with `It adds a marked block to ~/.zshrc, ~/.bashrc and your bash login file.`

`cli.rs` lines 102-105 (the claude loop) gain `--bg`:

```rust
        let refused = tool == "claude"
            && (arg == "--print"
                || arg == "--bg"
                || arg.starts_with("--print=")
                || (arg.len() > 1 && arg.starts_with('-') && !arg.starts_with("--") && arg[1..].bytes().all(|b| b.is_ascii_alphabetic()) && arg.contains('p')));
```

Add one comment line above it: `// --bg starts a background job and returns: the job is shared through \`claude attach\` (spec §4 row c), so the real tool runs.`

- [ ] **Step 4: Run.**
Run: `pnpm vitest run src/components/desktop/__tests__/LocalSessions.test.tsx src/components/desktop/__tests__/ExplicitSharingNotice.test.tsx src/components/desktop/__tests__/DesktopSettings.test.tsx`, `pnpm exec tsc --noEmit`, `pnpm lint` (from `apps/frontend`); the Rust command from Step 2 with `--lib cli`.
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add apps/frontend/src/components/desktop/DesktopPermissionsGate.tsx apps/frontend/src/components/desktop/LocalSessions.tsx apps/frontend/src/components/desktop/ExplicitSharingNotice.tsx apps/frontend/src/components/desktop/__tests__/LocalSessions.test.tsx apps/desktop/src-tauri/src/cli.rs
git commit -m "feat(desktop): Share every session live is on at pairing; lightbulb claude --bg runs the real tool (S4)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Phone state copy (S5, S6)

The phone keeps its history list (S3 replaces it). Only the state copy changes.

**Files:**
- Modify: `apps/mobile/src/app/(app)/session/[sessionId].tsx:3-6 (import), 35 (import), 40-44, 403-408`
- Test: `apps/mobile/src/app/(app)/__tests__/session.test.tsx` (append inside `describe("session screen")`)

**Interfaces:**
- Consumes: `terminalStateReason` (Task 5); `relativeTime` (`apps/mobile/src/lib/relativeTime.ts`).

- [ ] **Step 1: Write the failing tests:**

```tsx
  it("says Ended with how long ago", async () => {
    mockSession = { ...base, status: "ended", endedAt: new Date(Date.now() - 5 * 60_000).toISOString() };
    mockLive.messaging.getLocalSession.mockResolvedValue(mockSession);
    await render(<SessionScreen />);
    expect(await screen.findByText("Ended · 5m")).toBeTruthy();
  });

  it("tells a teammate how a plain or a no-daemon session gets its screen", async () => {
    mockSession = { ...base, humanUserId: "user-b" };
    await render(<SessionScreen />);
    expect(await screen.findByText("Ask Bob to press ← in their terminal.")).toBeTruthy();
    cleanup();
    mockScope += 1;
    mockSession = { ...base, humanUserId: "user-b", provider: "codex", terminalReason: "no-daemon" };
    await render(<SessionScreen />);
    expect(await screen.findByText("Ask Bob to start it again with lightbulb codex.")).toBeTruthy();
    expect(screen.queryByText(/press ←/)).toBeNull();
  });
```

- [ ] **Step 2: Run to verify it fails.**
Run (from `apps/mobile`): `pnpm test -- "src/app/(app)/__tests__/session.test.tsx"`
Expected: the two new tests FAIL.

- [ ] **Step 3: Implement.** Add `terminalStateReason` to the `@lightbulb/shared` import (lines 3-6) and `import { relativeTime } from "../../../lib/relativeTime";` beside the other `lib` imports.

`sessionWhere` (lines 40-44):

```tsx
function sessionWhere(session: LocalSession, own: boolean, ownerName: string): string {
  const mac = own ? "your Mac" : `${ownerName}’s Mac`;
  if (session.status === "offline") return `${mac[0].toUpperCase()}${mac.slice(1)} is offline`;
  if (session.status === "ended" && session.endedAt) return `Ended · ${relativeTime(Date.parse(session.endedAt))}`;
  return `${sessionStatus[session.status]} on ${mac}`;
}
```

Lines 403-408 become (declare `const reason = terminalStateReason(session);` beside `const ended = …` at line 331):

```tsx
      {reason !== null && (
        // No terminal here yet: what gets its screen shared (spec 2026-10-07 §4 f, g).
        <Text style={[styles.meta, styles.liveHint, { color: palette.fgMute }]} accessibilityLiveRegion="polite">
          {reason === "plain"
            ? own ? "Press ← in its terminal to share this one live, or turn on Share every session live in Settings to make every new session live." : `Ask ${ownerName} to press ← in their terminal.`
            : own ? "Start it again with lightbulb codex to share its screen." : `Ask ${ownerName} to start it again with lightbulb codex.`}
        </Text>
      )}
```

- [ ] **Step 4: Run.**
Run: `pnpm test -- "src/app/(app)/__tests__/session.test.tsx"`, `pnpm typecheck`, `pnpm lint` (from `apps/mobile`).
Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add "apps/mobile/src/app/(app)/session/[sessionId].tsx" "apps/mobile/src/app/(app)/__tests__/session.test.tsx"
git commit -m "feat(mobile): Ended time and the plain / no-daemon lines on the session screen (S5, S6)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Skill text

**Files:**
- Modify: `apps/infra/hermes/skills-src/lightbulb/references/surfaces.md:96-103, 115-117`
- Modify: `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md:140`

- [ ] **Step 1: `surfaces.md` lines 96-103.** From "The owner can pause one session…" to "…with `lightbulb_local_sessions`." replace with:

```markdown
`codex` in any terminal. The owner can pause one session or turn sharing off for the Mac. When a
person pairs a Mac, **Share every session live** starts on (they can turn it off there or later
in Settings), so a plain `claude` or `codex` typed in a new terminal tab runs through Lightbulb.
A running session shows its live terminal when Lightbulb started it, when it is a Claude Code
background session, or when it is a plain Codex session (Codex's own background service runs
it, and Lightbulb opens one more Codex terminal on the same conversation). Any other running
session shows a notice that it was not started through Lightbulb, with the one thing that brings
its screen here: for a plain Claude session its owner presses ← in its terminal; for a Codex
session started with `--no-daemon` its owner starts it again with `lightbulb codex`. On the web
and in the desktop app people never see a transcript; the phone still shows a session's history
as a list until it gets the live terminal. You still read the structured record with
`lightbulb_local_sessions`.
```

- [ ] **Step 2: `surfaces.md` lines 115-117.** Replace "An ended session shows "Ended" with Resume, and a session whose Mac is offline shows "Mac offline" with the follow-up box." with:

```markdown
An ended session that Lightbulb hosted keeps its final screen, read-only, under "Ended · <time>"
with Resume, for as long as the owner's Lightbulb app runs; once the app quits or restarts the
panel says the screen is gone, still with Resume. Nothing of the screen is saved anywhere. A
session whose Mac is offline shows "Mac offline" with Resume greyed out, the follow-up box, and
**Pick up** for a teammate when its owner allows pick-up.
```

- [ ] **Step 3: `MAINTENANCE.md` line 140** becomes:

```text
  - Session live vs transcript  references/surfaces.md (running = live terminal or a "not started through Lightbulb" notice with ← (plain Claude) or `lightbulb codex` (Codex --no-daemon); ended = final screen + "Ended · <time>" + Resume while the owner's app runs, then "screen is gone"; offline = "Mac offline" + Resume greyed + Pick up; Share every session live on at pairing; no transcript on web and desktop (the phone still lists history); sidebar lists live, All sessions lists every session; no message box under a live terminal)
                              <- apps/desktop/src-tauri/src/holder/run.rs (ended mode), local_sessions/session_states.rs,
                                 apps/backend/core/services/{local_sessions,session_screen}.py (screenAvailable, terminalReason),
                                 apps/frontend/src/components/desktop/DesktopPermissionsGate.tsx (live default).
```

- [ ] **Step 4: Check the build.** Run (from the worktree root): `bash apps/infra/hermes/build-skills.sh` only if it writes inside the worktree's gitignored `apps/infra/hermes/skills/`; otherwise skip. Confirm `git status` shows no file under `apps/infra/hermes/skills/`.

- [ ] **Step 5: Commit.**

```bash
git add apps/infra/hermes/skills-src/lightbulb/references/surfaces.md apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md
git commit -m "docs(skills): ended screens, offline Resume and Pick up, reason lines, live by default

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

The skill reaches agents only through a Hermes pin PR plus a runtime roll, with the owner's yes. Not in this branch.

---

### Task 10: Prod acceptance (manual, after a desktop build)

No desktop dev build exists: run this on PROD as the founder (founder Mac) with a second account (Isol8 Admin in Chrome), after the backend + web prod dispatch AND a desktop build carrying Tasks 1, 2 and 7. Record each line PASS / FAIL with a screenshot on the PR.

- [ ] **Step 1: Baseline for "nothing recorded".** On the founder Mac: `touch /tmp/states-marker`; note the UTC time.

- [ ] **Step 2: S4 Accept.** Re-pair the founder Mac (or pair a test account's Mac): the pairing dialog shows **Share every session live** pressed, naming `~/.zshrc, ~/.bashrc and your bash login file`. Continue. Open a NEW terminal tab, run `claude`: the session is held and shows a live terminal on the web for the second account. Settings → Desktop shows the switch on. Turn it off: `grep -c "lightbulb live sessions" ~/.zshrc ~/.bashrc` prints 0 for each. Turn it back on. Run `lightbulb claude --bg "say hi"`: the real tool runs (no "Shared with" line, a background job starts) and the job appears as an attach session.

- [ ] **Step 3: S5 Accept — ended screen.** Start a held session from the web (New session), type a prompt, wait for an answer. As the second account, open it. In the founder's terminal end it (`/exit`). The teammate sees the final screen with "Ended · <time>" above it and Resume below it, and cannot type into it. Resume (as the founder): the panel switches to the live terminal; within 10 s `ls /private/tmp/lightbulb-sessions-$(id -u)/h/` no longer lists a listening socket for the old holder (`lsof -U | grep lightbulb-sessions` shows one holder for the session).

- [ ] **Step 4: S5 Accept — offline.** With the resumed session running, quit the Lightbulb app. Within 40 s the teammate sees "Prasiddha's Mac is offline" with Resume greyed and, when pick-up is allowed for that session, Pick up. Relaunch the app.

- [ ] **Step 5: S5 Accept — relaunch.** End a held session, confirm the teammate sees its final screen, then quit and relaunch the app. Within 45 s the teammate's panel reads "The screen is gone: this session's terminal closed." with Resume.

- [ ] **Step 6: Nothing written.** `find ~/Library/Application\ Support/company.thelightbulb.desktop -newer /tmp/states-marker -type f` lists only the existing `local-sessions/*.json` mirror files (no new kind of file), and none contains the final screen's ANSI bytes (`grep -l $'\x1b\[' <those files>` prints nothing new). `aws s3api list-objects-v2 --profile isol8-admin --bucket lightbulb-prod-openclaw-replicas --prefix <founder org id>/ --query "Contents[?LastModified>='<marker UTC>'].Key"` lists no session-screen object.

- [ ] **Step 7: S6 Accept.** Turn the switch off, open a NEW terminal tab, run plain `claude`, send a prompt: the teammate sees "This session was not started through Lightbulb, so its screen is not here." and "Ask Prasiddha to press ← in their terminal." and no rows. Press ← in that terminal: the panel switches to the attach terminal. Run `codex --no-daemon`, send a prompt: the teammate sees "Ask Prasiddha to start it again with `lightbulb codex`.". Turn the switch back on.

- [ ] **Step 8: Agent unaffected.** As the founder, ask an agent to `lightbulb_local_sessions read` the ended session: it returns events.

---

## Rollout

Order does not matter for correctness; each part degrades to S1 without the others:

- **Backend before desktop** (expected order): the relay lists `endedScreens`/`terminalReason`, but an older Mac never sends them, so nothing changes. Its holders still exit with the provider.
- **Desktop before backend:** the Mac sends no new field until a registration ack lists it (Task 2 gates on `read_features`). Ended holders still stay alive (memory only) until the app relaunches or the orphan sweep closes them; nobody can watch them. `resume::prepare` ignores them, so Resume works.
- **Web before backend:** `screenAvailable` and `terminalReason` are absent; the panels behave as S1.
- **Backend before web:** an ended row may carry `terminal: "held"`, but S1's web checks `running` before watching, so it shows "The screen is gone".
- The prod promotion should still be backend + web first (one dispatch), then the desktop build, so the Accept run (Task 10) sees the whole feature at once. A merge touching `packages/shared` or `apps/mobile` ships an iPhone OTA; the phone change is copy only.
