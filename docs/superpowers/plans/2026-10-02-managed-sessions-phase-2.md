# Managed Sessions Phase 2: Holder and Local Terminal Window

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The owner can open a real Claude Code or Codex terminal in a Lightbulb desktop window; the session survives the app quitting or updating. Nothing about sharing changes in this phase.

**Architecture:** The holder is a mode of the app's own binary (`lightbulb-desktop holder …`, like the existing `session-mcp` and `local-session-hook` subcommands), so it is signed with the app and needs no extra bundled binary. It owns a pty, a `vt100` terminal model and an offset-addressed output log, and serves one interactive client plus output-only clients on a unix socket. The app starts holders detached, finds them by their sockets, and shows each in a terminal window served from a custom `lbterm://` scheme with vendored xterm.js. Holder commands refuse any caller that is not a terminal window.

**Tech Stack:** Rust (Tauri 2, `portable-pty` 0.9, `vt100` 0.16, `nix`), xterm.js 5.5 + fit addon 0.10 (vendored, MIT).

**Spec:** `docs/superpowers/specs/2026-10-02-managed-session-sharing.md` §5.1-5.2. Parent plan: `2026-10-02-managed-session-sharing.md`. Spike code: branch `spike/managed-sessions`, `scratch/managed-sessions/holder/src/main.rs`.

## Global Constraints

- Provider runs unmodified and interactive; started directly with the login shell's captured environment (`$SHELL -l -i -c 'env -0'`, stdin closed, `DISABLE_AUTO_UPDATE=true`), never through an interactive shell.
- Only a terminal window may call `holder_input`, `holder_resize`, `holder_attach`, `holder_start`, `holder_list`. The main webview (remote origin) may only call `terminal_open`.
- Shift+Enter in the terminal window sends `\x1b\r`.
- One interactive client per holder; the newest attach wins and the previous receives `DETACHED`.
- Socket directory `/private/tmp/lightbulb-sessions-<uid>/h/`, mode 0700; sockets 0600.
- Tests are written with the code and run in CI (`test-desktop` on the self-hosted Mac). No local full-suite runs.
- No change to discovery, mirroring, the relay or the backend in this phase.

## Review Focus

1. A three-line bracketed paste through `holder_input` arrives byte-for-byte (Task 3 test).
2. A client attaching to a session with no previous client gets a snapshot with the current modes (Task 2 test).
3. The main webview calling `holder_input` is refused (Task 5 test).
4. A holder whose provider exits removes its socket and the manager stops listing it (Task 4 test).
5. Two attaches: the first receives `DETACHED`, only the second's input reaches the pty (Task 3 test).

---

### Task 1: Frame protocol

**Files:**
- Create: `apps/desktop/src-tauri/src/holder/mod.rs`, `apps/desktop/src-tauri/src/holder/protocol.rs`
- Modify: `apps/desktop/src-tauri/src/lib.rs` (`pub mod holder;`)

**Interfaces (produced):**
```rust
pub const HELLO: u8 = 1; pub const OUTPUT: u8 = 2; pub const INPUT: u8 = 3; pub const RESIZE: u8 = 4;
pub const EXIT: u8 = 5; pub const REPLAY: u8 = 6; pub const GAP: u8 = 7; pub const SNAPSHOT: u8 = 8;
pub const DETACHED: u8 = 9;
pub fn encode(kind: u8, payload: &[u8]) -> Vec<u8>;
pub fn read_frame(r: &mut impl std::io::Read) -> std::io::Result<(u8, Vec<u8>)>; // rejects payload > 8 MiB
```

- [ ] **Step 1:** Write tests: `encode` then `read_frame` round-trips each kind; an empty payload works; a declared length over 8 MiB returns `InvalidData` without allocating it.
- [ ] **Step 2:** Implement (header `[u8 kind][u32 BE len]`), as in the spike's `frame()` plus a bounded reader.
- [ ] **Step 3:** Commit `feat(desktop): holder frame protocol`.

### Task 2: Terminal model

**Files:**
- Create: `apps/desktop/src-tauri/src/holder/model.rs`
- Modify: `apps/desktop/src-tauri/Cargo.toml` (`vt100 = "0.16"`)

**Interfaces (produced):**
```rust
pub struct Model { /* vt100::Parser */ }
impl Model {
    pub fn new(rows: u16, cols: u16) -> Self;
    /// Feed provider output. Returns the bytes to write back to the pty: answers to the
    /// probes in PROBES (DA1, XTVERSION, OSC 10/11, CPR from the model's cursor).
    pub fn feed(&mut self, bytes: &[u8]) -> Vec<u8>;
    pub fn resize(&mut self, rows: u16, cols: u16);
    /// `\x1b[?1049h` when on the alternate screen, then `state_formatted()`.
    pub fn snapshot(&self) -> Vec<u8>;
}
/// A client's probe reply (DA, XTVERSION, OSC 10/11 colour, CPR, kitty flags): dropped.
pub fn is_probe_reply(input: &[u8]) -> bool;
```

- [ ] **Step 1:** Tests: `feed(b"\x1b[?u\x1b[c")` returns `b"\x1b[?62;22c"`; `feed(b"\x1b]11;?\x07")` returns the OSC 11 answer with `\x07`; after `feed(b"\x1b[?2004h")` the snapshot contains `\x1b[?2004h`; after `feed(b"\x1b[?1049hX")` the snapshot starts with `\x1b[?1049h`; `is_probe_reply` is true for `\x1b[?62;22c`, `\x1b[12;40R`, `\x1b]11;rgb:0/0/0\x07`, and false for `\x1b[A` and `hello`.
- [ ] **Step 2:** Implement from the spike's `PROBES` table and `is_probe_reply`, with the CPR answer computed from `screen().cursor_position()`.
- [ ] **Step 3:** Commit `feat(desktop): holder terminal model and probe answers`.

### Task 3: The holder process

**Files:**
- Create: `apps/desktop/src-tauri/src/holder/run.rs`
- Modify: `apps/desktop/src-tauri/src/main.rs` (dispatch `holder`), `Cargo.toml` (`portable-pty = "0.9"`)
- Test: `apps/desktop/src-tauri/tests/holder.rs`

**Interfaces (produced):**
```rust
/// `lightbulb-desktop holder <socket> -- <argv…>`; runs until the provider exits.
pub fn run(socket: &Path, argv: &[String], cwd: &Path) -> std::io::Result<()>;
pub fn login_environment() -> Vec<(String, String)>; // captured once, see Global Constraints
```
`HELLO` reply JSON: `{"protocol":1,"pid":u32,"providerPid":u32,"argv":[..],"cwd":"..","rows":u16,"cols":u16}`. `OUTPUT` payload: `u64 BE offset` + bytes. Log cap 1 MiB.

- [ ] **Step 1:** Integration tests driving `run` in a thread with `argv = ["/bin/cat"]` and a temp socket path:
  - attach `interactive`, send `INPUT b"\x1b[200~a\nb\nc\x1b[201~"`, read `OUTPUT` until it contains `a`, `b`, `c` (the tty echoes them);
  - attach a second `interactive`: the first receives `DETACHED`; input from the first is ignored;
  - `REPLAY` from offset 0 returns the bytes so far; from an offset older than the log returns `GAP`;
  - send `INPUT b"\x04"` (EOF to cat): every client receives `EXIT` and the socket file is gone.
- [ ] **Step 2:** Implement from the spike's `main.rs` (event loop, clients map, interactive slot, offset log), plus: the socket directory created 0700, `login_environment()` from the spike's capture, and the `holder` dispatch in `main.rs` before `run()`.
- [ ] **Step 3:** Commit `feat(desktop): holder process`.

### Task 4: Holder manager

**Files:**
- Create: `apps/desktop/src-tauri/src/holder/manager.rs`
- Modify: `Cargo.toml` (`nix` features `process`)

**Interfaces (produced):**
```rust
pub struct HolderInfo { pub id: String, pub socket: PathBuf, pub pid: u32, pub provider_pid: u32,
                        pub argv: Vec<String>, pub cwd: PathBuf }
pub fn socket_dir() -> PathBuf;                       // /private/tmp/lightbulb-sessions-<uid>/h
/// Spawns `current_exe() holder <dir>/<id>.sock -- argv` in a new session (setsid in pre_exec),
/// stdin/out/err null, not waited on; returns once the socket accepts.
pub fn start(cwd: &Path, argv: &[String]) -> Result<HolderInfo, String>;
/// Connects to each socket, reads HELLO as a recorder; unlinks sockets that refuse.
pub fn list() -> Vec<HolderInfo>;
```
`id` is 8 hex characters (keeps the socket path under 104 bytes).

- [ ] **Step 1:** Tests (with `CARGO_BIN_EXE` of the app binary is not available for a lib test, so test `start` through a small helper that takes the program path; use `/bin/cat`-backed `run` from Task 3 via the test binary's own `holder` mode): `start` then `list` shows one entry with the right argv; killing the provider leads `list` to drop it and remove the socket; a stale socket file with no listener is unlinked by `list`.
- [ ] **Step 2:** Implement.
- [ ] **Step 3:** Commit `feat(desktop): holder manager`.

### Task 5: Terminal window and guarded commands

**Files:**
- Create: `apps/desktop/src-tauri/terminal/index.html`, `terminal.js`, and vendored `xterm.js`, `xterm.css`, `addon-fit.js` (xterm 5.5.0, addon-fit 0.10.0, from npm, with their MIT LICENSE)
- Create: `apps/desktop/src-tauri/src/holder/window.rs`
- Modify: `apps/desktop/src-tauri/src/lib.rs` (`register_uri_scheme_protocol("lbterm", …)`, commands in `generate_handler!`), `apps/desktop/src-tauri/capabilities/terminal.json`

**Interfaces (produced):**
```rust
/// Callable from the main webview: opens (or focuses) a terminal window.
/// `holder_id: None` opens the new-session form in that window.
#[tauri::command] pub fn terminal_open(app: AppHandle, holder_id: Option<String>) -> Result<(), String>;
/// Callable only when `is_terminal_caller(webview)`:
#[tauri::command] pub fn holder_list(webview: Webview) -> Result<Vec<HolderInfo>, String>;
#[tauri::command] pub fn holder_start(webview: Webview, cwd: String, provider: String) -> Result<HolderInfo, String>;
#[tauri::command] pub fn holder_attach(webview: Webview, id: String, on_frame: Channel<Vec<u8>>) -> Result<(), String>;
#[tauri::command] pub fn holder_input(webview: Webview, id: String, data: Vec<u8>) -> Result<(), String>;
#[tauri::command] pub fn holder_resize(webview: Webview, id: String, cols: u16, rows: u16) -> Result<(), String>;
/// Window label starts with "terminal-" AND the webview URL scheme is "lbterm".
pub fn is_terminal_caller(label: &str, url: &url::Url) -> bool;
```
`provider` is `"claude"` or `"codex"` only; `cwd` is chosen with the dialog plugin's folder picker in the terminal window.

- [ ] **Step 1:** Tests: `is_terminal_caller("terminal-ab12", lbterm://localhost/)` true; `("main", https://thelightbulb.company/chat)` false; `("terminal-ab12", https://evil.example/)` false; `("main", lbterm://localhost/)` false. `holder_start` with provider `"bash"` is refused.
- [ ] **Step 2:** Implement the scheme handler serving the embedded files (`include_bytes!`, correct content types), the window builder (`label = "terminal-<id|new>"`, URL `lbterm://localhost/index.html?id=<id>`), and the commands. `holder_attach` opens an interactive connection and forwards `SNAPSHOT`/`OUTPUT`/`DETACHED`/`EXIT` frames over the Channel on a thread.
- [ ] **Step 3:** `terminal.js`: xterm with `macOptionIsMeta: true`, fit addon, Shift+Enter → `\x1b\r` via `attachCustomKeyEventHandler`, `onData` → `holder_input`, window resize → `holder_resize`; new-session form (Choose folder, Claude Code / Codex, Start) when no `id`.
- [ ] **Step 4:** `capabilities/terminal.json` grants the terminal windows `core:default` and `dialog:allow-open` only; `default.json` stays scoped to `main`.
- [ ] **Step 5:** Commit `feat(desktop): local terminal window`.

### Task 6: Entry point in the app

**Files:**
- Modify: the web app's Sessions sidebar header (`apps/frontend/src/components/…`, located during the task with `rg -n "Sessions" apps/frontend/src/components/messaging`): a "New terminal session" button shown only inside the desktop app (`window.__TAURI__` present) that calls `invoke("terminal_open", { holderId: null })`.
- Test: the button is absent without `window.__TAURI__` and calls `terminal_open` with it.

- [ ] **Step 1:** Write the test, implement, commit `feat(web): open a terminal session from the desktop app`.

### Acceptance (prod, founder, after promotion)

Phase 1b tasks 1.5-1.7 of the parent plan, run in this build: update survival, macOS identity, volume. Plus: open a terminal window, start Claude Code, close the window, reopen it from the list, and continue the same session.

Moved to Phase 3: retargeting remote start and pick-up to holders (they also change sharing behaviour).
