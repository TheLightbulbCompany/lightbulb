# Split `lightbulb-core` out of the desktop crate

Date: 2026-10-22. Source: `origin/main` at `84ebf5447`. This is a plan only: no code, branch or push.
Line references are to that commit, under `apps/desktop/src-tauri/` unless a path says otherwise.

**Goal.** Move the cross-agent session network and the `lightbulb` CLI out of the Tauri crate
`lightbulb-desktop` into a new crate, `lightbulb-core`. The new crate builds without Tauri, and
the app depends on it. The owner chose what goes in: local session discovery and delivery
(Claude Code, Codex, Cursor, Grok CLI), the hooks entry points, the CLI, headless `join`/`serve`,
device login and pairing state, and the relay connection to the hub. The window, agent Chrome,
screen sharing, the agent's file tools, the Mac node, the updater and the tray stay private.

**Shape of the answer.** About 40k of the crate's 53.6k lines move. Most of the work is
mechanical, because the session code is already almost free of Tauri:
`local_sessions.rs` and `transport.rs` (12k lines) have no `tauri` at all. The coupling sits
in five places: `enrollment.rs` (Tauri commands and `AppHandle`), `notify.rs` (the ObjC
delegate holds an `AppHandle`), `session_start.rs` (core and app halves in one file),
`holder/panel.rs` (one getter), and two constants in `session_browser.rs`. We invert them with
**one new callback and no new traits**. Most of the work is in-place decoupling PRs, then one
pure-rename PR.

---

## 1. The boundary

Legend: **core** = moves to `lightbulb-core`. **app** = stays in `lightbulb-desktop`.
**split** = the file is mixed, and the table says how it is cut.

### `src/`

| File | Lines | Goes to | Reason |
|---|---|---|---|
| `main.rs` | 53 | **app, thin** | The `print-env`/`serve`/`holder`/`session-mcp`/`local-session-hook` dispatch (`main.rs:4-51`) moves to `lightbulb_core::entry::dispatch(&argv) -> Option<i32>`. Both binaries call it. `main.rs` keeps only `lightbulb_desktop::run()`. |
| `lib.rs` | 2109 | **app** | Tauri builder, window, deep links, updater, node host. It loses `log()` (`:127`, moves to core) and the `KEYCHAIN_SERVICE`/`LIGHTBULB_API_BASE` consts (`:43`, `:47`, move to core `config`). It gains `pub use lightbulb_core::{…}` re-exports so `crate::local_sessions::…` paths in the app keep compiling. |
| `tray.rs`, `agent_cast.rs`, `agent_chrome.rs`, `screen.rs`, `node_invoke.rs`, `local_mcp.rs`, `subprocess.rs`, `proxy.rs`, `bootstrap.rs`, `claude_export.rs`, `desktop_permissions.rs`, `process_owner.rs` | ~9.3k | **app** | Owner list: window, agent Chrome, screen, Mac node host (`bootstrap`→`proxy`→`subprocess`→`local_mcp`), marketplace export, consent at pairing, the window's single-instance guard. `serve` uses `serve.lock` (`headless.rs:22`), not `process_owner`. |
| `node_client.rs` | 1318 | **app, minus ~30 lines** | The Mac node client stays. `derive_device_id` and `raw_public_key_from_pem` (`:756-768`) move to core `device_token.rs`: `pair_state.rs` (4 uses), `enrollment.rs` (3) and `headless.rs:672-673` need them, and they are pure crypto. |
| `session_browser.rs` | 1048 | **app, minus 2 consts** | The Lightbulb browser for shared sessions rides agent-Chrome channels (`crate::agent_chrome::test_channels` ×11). `PORT` (`:40`) and `NOT_SHARED` (`:19`) move to core `local_sessions/browser.rs`, because `native_mcp.rs:45` and `native_tools.rs:162` use them. With no app on the machine, `native_mcp::browser_command` already returns `None` (no bundled node), so a core-only install adds no browser MCP. |
| `notify.rs` | 501 | **split** | Core gets `Notice`, the `Center` trait and its `Native` impl (`:38-91`), `act` (`:93`), `REQUEST_CATEGORY`/`ALLOW`/`DECLINE`, `session_click` (`:37`), `native_notify` and `notification_permission` as plain async fns, `install_headless`, `ask_once` (`:117`), and the whole `macos` module (`:206-472`). The `Delegate` ivar `app: Option<AppHandle>` (`:229-232`) becomes `on_click: Option<Box<dyn Fn(&str, &BTreeMap<String,String>) + Send + Sync>>`. The app keeps its click routing as that closure (`:270-291`: `agent_chrome::SHOW_TAG` → `raise_from_click`, `focus_or_recreate_main_window`, `emit("notification:clicked", session_click(..))`) and the two `#[tauri::command]` wrappers. |
| `file_access.rs` | 360 | **split** | The allowlist **store** goes to core as `approved_folders.rs`: `FileAccessStore`, load/save, `init`, `default_seed_path`, `canonicalize` (`:104`), `is_inside` (`:129`), `list_paths` (`:194`), `add_path`/`remove_path` (`:243`, `:263`). The session side needs it: `resume.rs:286-298` refuses a folder that is not approved, and `session_start`/`session_pickup` canonicalize against it. The agent-tool side stays in the app: `execution_policy` (`:202`), `ensure_inside_allowlist` (`:282`), and the four `file_access_*` commands in `lib.rs:1250-1295`. Both sides read the same `file_access.json`. **Owner decision 3.** |
| `session_start.rs` | 740 | **split** | To core as `local_sessions/launch.rs`: `tools` (`:127`), `validate`, `argv`, `open_argv`/`validate_open`, `start_held` (`:398`), `held_argv`, `approved_now`/`inside_approved` (`:446-456`), `cli_argv`, and `cli_start` (`:485`). These are what `lightbulb claude`, `resume` and pick-up use. Stays in the app as `session_start.rs`: `start` (`:342`, the node rail's `session.start.v1` from `node_invoke`), `options` (`:169`), `shared_scope`, `mac_name`, and the `terminal_options`/`terminal_start` commands (`:426`, `:434`, the panel's New session). The source-scan test at `:667` (`include_str!("session_start.rs")`) must scan both files. |
| `session_pickup.rs` | 1415 | **core, minus the command** | Receiver-side pick-up. `local_sessions/snapshot.rs:9` imports its git helpers and limits, and `session_continue` is built on it. Only the `#[tauri::command] session_pickup` (`:983-1010`) moves to the app as a wrapper. **Owner decision 2.** |
| `session_continue.rs` | 857 | **core** | `lightbulb continue '<link>'`, a CLI command (`cli.rs:540-548`). It has no Tauri. **Owner decision 2.** |
| `shell_integration.rs` | 166 | **core, minus 2 commands** | `lightbulb share on|off` (`headless.rs:474`), `join`/`leave`, and `transport.rs:658` (`is_on`). The `shell_integration_status`/`_set` commands (`:97-110`) move to the app as wrappers. |
| `cli.rs` | 952 | **core** | `join/setup/share/leave/continue` and the `claude`/`codex` wrappers (`:527-588`). `serve(app: AppHandle)` (`:194`) becomes `serve(data: &Path)`: its only use of the handle is `enrollment::state_dir(&app)` (`:200-201`) through `cli_start`. `set_workspace` (`:183`) stays a static in core. The app's command calls it. |
| `headless.rs` | 791 | **core** | `serve`, `join`, `setup`, `leave`, the launch agent. `tauri::async_runtime::block_on` (12 uses) becomes core `rt::block_on`. `app_data_dir()` (`:15`) moves to core `paths.rs`, and the window uses it as well (see §2). |
| `keychain.rs` | 191 | **core** | The app, `serve` and the CLI must read **the same** Keychain items, so this is a platform backend, not an app-injected trait. The `security-framework` store is `cfg(target_os = "macos")`. Linux gets a file backend (decision 4). |
| `device_token.rs`, `pair_client.rs`, `pair_state.rs` | 583 | **core** | Device login and pairing. `pair_client::DesktopPermissions` is part of the pair wire contract, and the app's `desktop_permissions.rs` keeps using it from core. |
| `holder/mod.rs`, `identity.rs`, `manager.rs`, `model.rs`, `protocol.rs`, `run.rs` | 3696 | **core** | **The holder belongs in core.** Held sessions are what `lightbulb claude` attaches to (`cli.rs:250-251`), what `resume` restarts (`resume.rs:216`), what the relay watches (`local_sessions/screen.rs`) and types into (`remote_input.rs`). `transport.rs` calls `holder::manager` 29 times. The holder process is `lightbulb[-desktop] holder …` (`manager.rs:219-233`, `current_exe`), so core's binary has to answer `holder`. |
| `holder/panel.rs` | 527 | **app** | The terminal inside the app's session panel: a Tauri child webview (`Window::add_child`), the loopback page and `include_bytes!` of `terminal/*` (`:25-29`). Its one export that core reads, `shown_holder()` (`:290`, read at `local_sessions/screen.rs:323`), is inverted by moving the `SHOWN` state into core `holder/shown.rs` (`set`/`get`). The panel writes it and the relay reads it. |
| `local_sessions.rs` + all 24 files under `local_sessions/` | 31450 | **core** | Discovery and delivery for all four providers (`claude.rs`, `codex.rs`, `hooked.rs` = Cursor + Grok), `transport.rs` (relay, journal), `held_notice`, `presence`, defang (`local_sessions.rs`, `hooked.rs`), `native_mcp`/`native_tools` (session-mcp), `plugin`, `mod_socket`, `approval`, `resume`, `snapshot`, `screen`, `remote_input`, `session_states`, `start_links`, `stats`, `workspace`, `lifecycle`. One file is split. |
| ↳ `local_sessions/enrollment.rs` | 2238 | **split** | Core keeps `Controller`, the hook entry `hook()` (`:686`), skills, reassert, the relay start, `local_state_dir`. Out to a new app file `src/session_commands.rs`: the six `#[tauri::command]`s (`:808-1180`: `local_sessions_auth/status/audience/set/repair/revoke`), `state_dir(app)` (`:617`) and `app_auth(app)` (`:621`, the only `crate::AuthState` read in the core set, `:624`). `refresh_relay`/`restore`/`watch_launch_agent` (`:217`, `:465`, `:306`) take `(data: &Path, jwt: String)` in place of `&AppHandle`. `tauri::async_runtime::{spawn, JoinHandle, block_on}` become `crate::rt::…`. |

Totals: about **40.4k lines to core** and **13.2k stay in the app**.

### Outside `src/`

| Path | Goes to | Reason |
|---|---|---|
| `tests/holder.rs`, `tests/cli.rs` | core | `CARGO_BIN_EXE_lightbulb-desktop` → `CARGO_BIN_EXE_lightbulb` (`cli.rs:63`, `holder.rs:625,649`). |
| `tests/keychain_test.rs`, `tests/pair_client_test.rs` | core | They test core modules. CI does not run them today (see §4). |
| `tests/fixtures/claude_classic_exit.json`, `codex_inline_status.json` | core `tests/fixtures/` | `include_str!("../../tests/fixtures/…")` from `holder/model.rs:951,1022,1590`. The relative path still holds after the move. |
| `examples/local_session_probe.rs` | core | It uses `local_sessions::{supported_claude_version, verify_claude_runtime, MAX_JSONL_ROW}`. |
| `tests/proxy_test.rs`, `subprocess_test.rs`, `*.mjs`, `*.cjs` | app | Node host and agent browser. |
| `terminal/`, `capabilities/`, `icons/`, `scripts/`, `tauri.conf.json`, `Info.plist`, `entitlements*.plist`, `build.rs` | app | Tauri and bundle. |

---

## 2. The dependency graph and how each edge is inverted

These are the `crate::` references from the core set into the app set at `84ebf5447`, from a
grep of `crate::<module>`. `super::` and in-set references are left out. **Every edge is
listed.**

| From (core) | To (app) | Uses | Inversion |
|---|---|---|---|
| `local_sessions/enrollment.rs:624` | `lib.rs` `AuthState` | the current Clerk JWT | A parameter. The app's `session_commands.rs` reads `AuthState` and passes `jwt: String`. |
| `enrollment.rs:617,621` + `session_start.rs:172,384,437,501` + `resume.rs:150` + `cli.rs:201` | Tauri `app.path().app_data_dir()` | the data dir | **A function, not a trait:** `lightbulb_core::paths::app_data_dir()`, which is today's `headless.rs:15`. On macOS Tauri resolves the identifier to the same `~/Library/Application Support/company.thelightbulb.desktop`. An app unit test asserts the two are equal, so a future identifier change fails loudly. |
| `local_sessions/held_notice.rs` (`:13,110,149,199,275`), `resume.rs:226`, `session_pickup.rs`, `session_start.rs:365`, `headless.rs:43,425` | `notify.rs` | `Center`, `Native`, `native_notify`, `install_headless`, `ask_once` | `notify` is split (§1). The objc code moves to core. **The one new callback:** `notify::install(on_click)`, which replaces the delegate's `AppHandle` ivar. It carries `focus_window`, `agent_chrome::raise_from_click` and Tauri `emit`. `serve` passes `None`, as it does today (`install(None)`, `notify.rs:110-114`). `Center` already exists (it is how `held_notice` tests itself), so it is reused, not added. |
| `notify.rs:276-288` (moving part) | `agent_chrome::SHOW_TAG`, `raise_from_click`, `focus_or_recreate_main_window` (`lib.rs:1636`), `app.emit` | click routing | Into the app's `on_click` closure. Core never names them. |
| `local_sessions/screen.rs:323` | `holder::panel::shown_holder` | which holder the panel shows (so `reap_attach` spares it) | The `SHOWN` state moves to core `holder::shown`. The panel calls `set`. No trait. |
| `local_sessions/native_mcp.rs:45`, `native_tools.rs:162,466-553` | `session_browser::{PORT, NOT_SHARED}` | two consts | Move the consts to core. `session_browser.rs` imports them back. |
| `local_sessions/resume.rs:216,279,286,298` | `session_start::{start_held, tools, approved_now, inside_approved}` | starting a held session | These are the core half of `session_start` (§1). |
| `resume.rs:297,416`, `session_start.rs:153,272,278,449,456,518`, `session_pickup.rs` | `file_access::{canonicalize, list_paths, is_inside}` | approved folders | These are the core half of `file_access` (§1). |
| `local_sessions/snapshot.rs:9,854` | `session_pickup::{git, git_run, anonymous_git, normalize_remote, CODE_MAX, SIDECARS_MAX, allowed_url}` | git and upload helpers | `session_pickup` moves to core. |
| `local_sessions/transport.rs:658` | `shell_integration::is_on` | "share every new session" | `shell_integration` moves to core. |
| `enrollment.rs:14,…`, `pair_state.rs` ×4, `headless.rs:672-673` | `node_client::derive_device_id` | the device id from a public key | The function moves to core `device_token`. `node_client` imports it. |
| `enrollment.rs:829` | `cli::set_workspace` | the page's workspace name | It is inside `local_sessions_auth`, which moves to the app. `cli` is core, so the edge points the right way. |
| `enrollment.rs:270,309,467,902` | `headless::{service_lock, launch_agent_path}` | `serve.lock` and the launch agent | `headless` is core. The edge stays inside core. |
| every core file | `crate::log` (`lib.rs:127`) | the log file | **A moved function, not a trait.** `lightbulb_core::log` writes the same `/tmp/lightbulb-desktop.log`. The app re-exports it as `crate::log`. |
| `enrollment.rs:147,163,334,…`, `held_notice.rs:200`, `native_tools.rs:372`, `mod_socket.rs:215`, `session_continue.rs:351,447`, `start_links.rs:40`, `headless.rs:404,409` | `crate::KEYCHAIN_SERVICE`, `crate::LIGHTBULB_API_BASE` (`lib.rs:43,47`) | identifier and API host | Core `config.rs`. The app re-exports both names (see §6 on #4283). |
| `enrollment.rs` (8 sites), `held_notice.rs` (2), `resume.rs:149`, `session_start.rs:403`, `headless.rs` (12) | `tauri::async_runtime::{spawn, spawn_blocking, block_on, JoinHandle}` | the shared tokio runtime | Core `rt.rs`: one global multi-thread runtime, the same shape as Tauri's. The app calls `tauri::async_runtime::set(lightbulb_core::rt::handle())` before `tauri::Builder`, so the window and core share **one** runtime, as they do today. (`set` exists in Tauri 2. Check that it panics only when called after init.) |

**Edges from the app into core** (these become core's public API, `pub(crate)` → `pub`):
`lib.rs` → `local_sessions::enrollment::Controller`, `cli::{serve, set_workspace, install_at_launch}`,
`holder::manager::stop_ended`, `resume::install_dir`, `notify::install`;
`session_browser.rs` → `local_sessions::{shared_session_for, session_still_shared, enrollment::session_scope}`, `holder::identity`;
`holder/panel.rs` → `local_sessions::{remote_input, screen::holder_for_session}`, `holder::protocol`;
`node_invoke.rs` → `notify::{native_notify, notification_permission}`, the core half of `session_start`;
`agent_chrome.rs`, `bootstrap.rs`, `proxy.rs`, `desktop_permissions.rs` → `notify`, `keychain`, `device_token`, `pair_client`, `pair_state`.
That is about 25 items that go from `pub(crate)` to `pub`. `holder::identity` is `pub(crate) mod` today (`holder/mod.rs:6`).

**What we deliberately do not add:** a `Keychain` trait (one backend per OS, which all
processes must share), a `Log` trait, an `AppDataDir` trait, a `FocusWindow` or `Emit` trait
(both live inside the one click callback), and a `Holder` trait (the holder is core).

---

## 3. Crate layout

- **Location:** `apps/desktop/core/` (package `lightbulb-core`, lib `lightbulb_core`). It is
  **exactly as deep as `src-tauri/`**, so every `include_str!("../../../../../packages/…")`
  in `local_sessions/plugin.rs:56-63`, `enrollment.rs:1194`, `transport.rs:5389,5482,6991-6999`,
  `screen.rs:1138`, `resume.rs:336` and `session_states.rs:466` still resolves. So does
  `plugin.rs:437` (`CARGO_MANIFEST_DIR/../../../packages/claude-plugin`). No path is rewritten.
- **Binary:** `[[bin]] name = "lightbulb"`, `src/main.rs` =
  `std::process::exit(lightbulb_core::entry::dispatch(&argv).unwrap_or_else(|| cli::usage()))`.
  `dispatch` checks the internal subcommands (`holder`, `serve`, `print-env`, `session-mcp`,
  `local-session-hook`) **before** `cli::cli_args` (`cli.rs:257`). This order is required: for
  a binary named `lightbulb`, `cli_args` takes every word, so `manager.rs:233`'s
  `lightbulb holder <sock>` would otherwise be parsed as a CLI command.
- **Cargo workspace:** a new `apps/desktop/Cargo.toml` with `members = ["src-tauri", "core"]`
  and `resolver = "2"`. One `Cargo.lock` moves to `apps/desktop/Cargo.lock`.
  **`[profile.release]` (`src-tauri/Cargo.toml`: strip, lto, codegen-units=1, panic=abort)
  must move to the workspace root:** cargo ignores profiles in non-root members, and prints
  only a warning.
- **Dependencies:** core takes serde/serde_json(preserve_order), tokio, reqwest(rustls),
  ed25519-dalek, jsonwebtoken, rand, sha2, hex, base64, regex, toml_edit, tar, flate2, walkdir,
  tokio-tungstenite, futures-util, uuid, url, urlencoding, unicode-normalization, lazy_static,
  open; `cfg(unix)`: libc, portable-pty, tungstenite, vt100; `cfg(target_os = "macos")`:
  security-framework, block2, objc2, objc2-foundation, objc2-user-notifications.
  **No tauri.** That rule enforces the boundary from the day of the move. The app keeps
  the tauri plugins and whatever its own modules still import.
- **Feature flags: none.** `macos-notify` would duplicate what `cfg(target_os = "macos")`
  already does in `notify.rs`. The real gate is "running inside an app bundle"
  (`notify.rs` `bundled()`), a runtime check that already turns notifications off cleanly.
  Add a feature only if someone needs a macOS build without the objc2 crates.
- **What builds and what works:**

  | | macOS | Linux |
  |---|---|---|
  | Compiles (`cargo build -p lightbulb-core`) | after PR 6 | after PRs 2 and 6 (the macOS-only libc calls behind `os.rs` with fail-closed stubs) |
  | CLI `join`/`leave`/`share`/`continue`, `serve`, relay, hooks, session-mcp, plugin install, holders, `lightbulb claude|codex` | yes, as today | after PR 8 (Linux port) |
  | Request notifications (`held_notice`) | yes, inside the app bundle | no banner. The band, web and phone still answer (`notify.rs` `Native`: "Not allowed … shows nothing and changes nothing else") |
  | Lightbulb browser for sessions | app only | no (`browser_command` → `None`) |

  Today Linux compiles nothing in this set. `libc::getpeereid` and `LOCAL_PEERPID` are not in
  libc for Linux (checked: libc 0.2.191 defines `getpeereid` only under bsd/apple, aix, nto and
  cygwin), and `native_tools.rs:45-89` and `cli.rs:143-146` call them with no `cfg`. Even where
  it compiles, `local_sessions.rs:1269-1273` returns "local sharing supports macOS only" for
  every process lookup, so discovery finds nothing.
- **How the app wires it:** `src-tauri/Cargo.toml` adds `lightbulb-core = { path = "../core" }`.
  `lib.rs` re-exports the moved modules (`pub use lightbulb_core::{cli, headless, holder, keychain, …}`),
  so app code keeps its `crate::…` paths. `run()` calls `tauri::async_runtime::set(rt::handle())`,
  `.manage(Controller::default())`, `notify::install(Some(app_on_click(handle)))`, `resume::install_dir(...)`,
  `cli::serve(&paths::app_data_dir())`, and registers the wrapper commands from `session_commands.rs`
  under the **same command names**. The hosted page calls those names, so they must not change.
  `main.rs` calls `entry::dispatch` and then `run()`.
- **Packaging on macOS: no second binary.** Keep the app executable as the CLI and the
  `serve` target, as today: `~/.local/bin/lightbulb` → `Lightbulb.app/Contents/MacOS/lightbulb-desktop`
  (`cli.rs:594-617`), and the launch agent runs the same path (`headless.rs:178-232`). The
  process therefore runs inside the bundle, `NSBundle.mainBundle` is the app, and
  `UNUserNotificationCenter` files banners under `company.thelightbulb.desktop`. Nothing about
  signing, notarisation, the updater tarball or TCC changes. The standalone `lightbulb` binary
  is for Linux and for people who build from source. On a Mac it works but has no bundle, so it
  logs `[notify] not running from an app bundle` and shows no banners. A minimal `.app` wrapper
  for it is **not** recommended. With its own bundle id it would be a second Notifications
  entry. With ours it would collide with the app. **Owner decision 6.**

---

## 4. Tests

**What moves with the code.** About 600 of the 617 `#[test]`/`#[tokio::test]` functions in the
candidate files move: `transport.rs` 115, `screen.rs` 51, `local_sessions.rs` 47, `claude.rs` 37,
`enrollment.rs` 33, `codex.rs` 27, `holder/model.rs` 23, `cli.rs` 17, and the rest. What stays:
`holder/panel.rs` 9, the `start`/`options` tests of `session_start.rs`, the `execution_policy`
tests of `file_access.rs`, and the `lib.rs` suites (`auth_action_tests`, `window_recovery_tests`,
and #4283's `flavor_origin_tests`). Integration tests `holder.rs` and `cli.rs`, the probe example,
`keychain_test.rs` and `pair_client_test.rs` move. Backend `test_local_session_relay.py` still
reads the same `packages/shared/fixtures/local-session-frames/`, unchanged.

**CI today** (`.github/workflows/desktop-build.yml:58-91`, TestDesktop, self-hosted Mac, PRs
on `apps/desktop/**` and `packages/claude-plugin/**`): `cargo test --lib`,
`--test holder --test cli`, `--example local_session_probe`. Two gaps exist already:
`keychain_test`/`pair_client_test`/`proxy_test`/`subprocess_test` never run in CI, and a change
to `packages/shared/fixtures/local-session-frames/**` does not trigger the job.

**CI after the split:**

1. **TestDesktop (Mac, unchanged runner):** `cargo test --workspace --lib`, then
   `-p lightbulb-core --test holder --test cli`, then `-p lightbulb-core --example local_session_probe`,
   still with `TAURI_CONFIG` stubbing the sidecars. Add `packages/shared/fixtures/local-session-frames/**`
   to its `paths`.
2. **New `desktop-core.yml`, `TestCoreLinux` on `${{ vars.CI_RUNNER }}`.** It lives in its own
   workflow, because `desktop-build.yml`'s `concurrency: desktop-build` would queue it behind a
   150-minute prod build. It runs on PRs touching `apps/desktop/core/**`, `apps/desktop/Cargo.*`,
   `packages/claude-plugin/**` and `packages/shared/fixtures/local-session-frames/**`. It runs
   `cargo test -p lightbulb-core --locked --lib` from PR 1. Until PR 8, a test that needs macOS
   is gated with `#[cfg(target_os = "macos")]` in the PR that moves it, and each PR lists those
   tests. Many are gated already: `holder/identity.rs`, `holder/manager.rs:510,590`, and
   `notify.rs:493`. From PR 6 the job also runs `cargo check -p lightbulb-core --all-targets`.
   From PR 8 it adds `--test holder --test cli`. It needs no Tauri, webkit or Xcode: `-p lightbulb-core`
   compiles only core's dependency tree.
3. Add a no-Tauri guard in PR 3. It is an app unit test with an `include_str!` list of the
   to-be-core files, and it asserts that none of them contains `tauri` or names an app module
   (`agent_chrome`, `node_client`, `node_invoke`, `session_browser::`, `desktop_permissions`,
   `AuthState`, `focus_or_recreate_main_window`, `holder::panel`). It starts with an allowlist
   of today's offenders. Each decoupling PR shortens the list, and PR 6 deletes the test, because
   core's `Cargo.toml` has no tauri and the compiler then enforces the rule.

---

## 5. Migration order

Each PR keeps the app building and shippable, and each leaves the hosted page's IPC command
names unchanged. Sizes are rough, as lines moved / lines written or changed.

| # | PR | Moves | Writes | Risk |
|---|---|---|---|---|
| 0 | **Prerequisite, not ours:** land #4283 (prod defaults, `capabilities/dev.json`, `LIGHTBULB_WS_URL` const) and settle dependabot #4268 (it edits `src-tauri/Cargo.lock`, which PR 1 moves). | — | — | — |
| 1 | **Workspace + skeleton + leaf modules.** `apps/desktop/Cargo.toml` workspace (with `[profile.release]`), lock moved, `.github/dependabot.yml:31` → `/apps/desktop`, `core/Cargo.toml`, `core/src/lib.rs`. Moves `keychain.rs` (store `cfg(macos)`, non-mac `Err` stub), `device_token.rs` (+ `derive_device_id`, `raw_public_key_from_pem` from `node_client.rs:756`), `pair_client.rs`, `pair_state.rs`, `log`. `tests/keychain_test.rs` and `pair_client_test.rs` move. The app re-exports them. `desktop-core.yml` Linux job added. **Lowest risk:** leaf files with no `crate::` imports except `keychain`/`log`, and `pair_client::pair` already takes `api_base` as an argument, so this PR needs neither `config` nor #4283. | ~0.85k | ~0.2k | Low. Only the workspace/profile/lock move can bite. Check with a local `cargo tauri build` and `cargo build --release` with no "profiles for the non root package will be ignored" warning. |
| 2 | **`os.rs` in core:** one `process(pid)`, `parent(pid)`, `peer(socket) -> (uid, pid)`, `process_birth` from the five copies (`local_sessions.rs:1226-1273`, `native_tools.rs:45-89`, `claude.rs:646-660`, `holder/identity.rs:15-114`, `cli.rs:143-146`). The macOS bodies are copied verbatim. Non-macOS returns `Err` everywhere (fail closed). App callers switch to `lightbulb_core::os`. | ~0.3k | ~0.25k | Low on macOS (same code). On other OSes `claude.rs:646` changes from "skip the peer check" to "refuse". That is intended. |
| 3 | **Holder to core**, minus `panel.rs`. `holder/shown.rs` takes `SHOWN` (`panel.rs:290`). `entry::dispatch` + `[[bin]] lightbulb` (holder, print-env). `main.rs` calls `dispatch`. `tests/holder.rs` and `tests/fixtures/*.json` move. Adds the guard test from §4.3. `holder` → `local_sessions::process_birth` (`manager.rs`) now goes through `os::process_birth`. | ~3.2k | ~0.15k | Low. `manager.rs:219` still `current_exe`s the app binary in the app. |
| 4 | **Decouple `notify` and the runtime, in place.** Split `notify.rs` (core part still in the app crate for now), the `on_click` closure, core `rt.rs` and `tauri::async_runtime::set`. All `tauri::async_runtime` uses in the core set → `rt`. `native_notify`/`notification_permission` become wrappers. | — | ~0.35k | Medium: the delegate (`LightbulbNotificationDelegate`) must be defined exactly once, or ObjC aborts at registration. Test the click paths on a real Mac: request Allow/Decline, session click, "Open the agent window". |
| 5a | **De-Tauri `enrollment`/`resume`/`cli`.** `session_commands.rs` (the six commands, `state_dir`, `app_auth`), `Controller` methods take `(data, jwt)`, `paths::app_data_dir()` replaces `headless.rs:15`, `resume::install(app)` → `install_dir`, `cli::serve(data)`. Add the equal-data-dir test. | — | ~0.5k | Medium: sign-in, sharing on/off, repair and revoke. Drive the panel on dev. |
| 5b | **Split `session_start` (core half → `local_sessions/launch.rs` while still in the app crate), `file_access` (→ `approved_folders.rs`), `session_browser` consts, and the `session_pickup`/`shell_integration` command wrappers.** The guard allowlist reaches zero. | — | ~0.45k | Low to medium. Every remaining edge is mechanical. |
| 6 | **The big move:** `git mv` the whole decoupled set into `core/src` (local_sessions tree, `cli`, `headless`, `notify` core, `shell_integration`, `launch`, `session_pickup`, `session_continue`, `approved_folders`, `config`, `paths`, `rt`), with `tests/cli.rs` and the probe example. Deps move in `Cargo.toml`. App re-exports. `pub(crate)` → `pub` for the ~25 items in §2. Delete the guard test. The Linux job adds `cargo check --all-targets`. Review it with `git diff -M90% --stat`: it should be renames plus a short list of edits. | ~36.5k | ~0.25k | Medium: the size, and conflicts with any open desktop PR. **Needs a short merge freeze on `apps/desktop/src-tauri/src/{local_sessions*,cli,headless,notify,session_*}.rs`** (decision 9). Do the move with a rerunnable script, so a rebase is "rerun", not a manual merge. |
| 7 | **CI tidy:** TestDesktop commands per §4.1, the paths fix, `keychain_test`/`pair_client_test` run on the Mac job. | — | ~0.05k | Low. |
| 8 | **Linux port:** `os.rs` Linux impls (`SO_PEERCRED`, `/proc/<pid>/stat` ppid and start time, `/proc/<pid>/exe`). Keychain file backend (decision 4). `paths::app_data_dir` → `$XDG_DATA_HOME/company.thelightbulb.desktop`. Socket base `/tmp/lightbulb-sessions-<uid>` on Linux (macOS keeps `/private/tmp`: `holder/manager.rs:38`, `enrollment.rs:637`, `local_sessions.rs:1103`). `serve` answers `cli.sock` where no window app exists, so the `lightbulb claude` wrapper works headless. `join` on Linux skips the `/Applications` link (`cli.rs:632-664`) and launchd (decision 5). The plugin skills' `curl --unix-socket "/private/tmp/…/mod.sock"` (`packages/claude-plugin/lightbulb/skills/{share,lightbulb-session-sharing}/SKILL.md`) → `/tmp/…`, which also resolves on macOS. Ungate the Linux tests. | — | ~0.8k | Medium, all new code. A Linux `serve` against dev with Claude Code and Codex must pass before it is announced. |
| 9 | **Export update** (see §7). | — | — | — |

PRs 1-3 are independent of 4-5b, and 4, 5a and 5b can merge in any order. PR 6 needs 2-5b.
PR 8 needs 6.

---

## 6. Risks

**Behaviour changes (each one is intended and listed here so a reviewer can check it):**
- The runtime goes from Tauri's to core's `rt`, shared through `tauri::async_runtime::set`.
  If `set` is called after Tauri initialises its runtime, it panics at launch, so it goes first
  in `run()`. `enrollment.rs:78-126` already builds separate runtimes for the mod and tool
  sockets. Those stay as they are.
- `entry::dispatch` puts internal subcommands first. So `lightbulb serve` through the
  `~/.local/bin` link starts `serve`, where today it prints usage. This is harmless.
- On other OSes, peer and process checks fail closed (PR 2). macOS does not change.
- The data dir comes from a core function, not from Tauri. The unit test in PR 5a pins them
  equal. If the identifier ever changes, the test fails.
- `[profile.release]` must move to the workspace root. If it does not, the shipped app quietly
  loses `panic = "abort"`, LTO and strip.

**The generated bundle and sidecar paths:**
- `native_mcp::browser_command` (`native_mcp.rs:51-60`) finds `Contents/Resources/bin` next to
  `current_exe()`, or `target/<profile>/bin` in dev. CI sets `CARGO_TARGET_DIR`, so packaged
  builds do not change. Local `pnpm dev`'s target dir moves from `src-tauri/target` to
  `apps/desktop/target`. Check that the dev browser MCP still finds the vendored node.
- Hooks and MCP entries point at `current_exe()` (`enrollment.rs:869,932,986`, `reassert_exe`
  `:1268`). If an app and a standalone `lightbulb` both run on one Mac, each moves the
  Claude/Codex/Cursor/Grok hooks to itself on every reassert, and they flap. Mitigation: on macOS
  the app executable is the only CLI (§3). The standalone build documents "do not install beside
  the app".
- `cli.rs:638-640` links only an app under `/Applications`, and `serve`'s `ExeStamp`
  (`headless.rs:144-163`) restarts on an updated binary. Neither changes for the app.

**`option_env!` compile-time URLs, and #4283:**
- Only `LIGHTBULB_API_BASE` crosses into core. `LIGHTBULB_WS_URL` (the Mac node,
  `lib.rs:275,1067`) and `LIGHTBULB_CALLBACK_URL` (`lib.rs:1556`) stay in the app. The relay
  door for sessions comes from `/desktop/pair`'s `gatewayUrl` (`enrollment.rs:158-212`), not
  from a const.
- #4283 changes the defaults to prod, adds a `LIGHTBULB_WS_URL` const and asserts
  `super::LIGHTBULB_API_BASE` in `flavor_origin_tests`. **Do not repeat any of it.** PR 1 does
  not touch the consts. When `config.rs` moves (PR 6), it carries #4283's prod default
  verbatim, and `lib.rs` keeps `pub(crate) use lightbulb_core::config::API_BASE as LIGHTBULB_API_BASE`,
  so #4283's test compiles unchanged. Capabilities and `tauri*.conf.json` stay app files.
  The split never edits them.
- `option_env!` in a dependency is read when that crate compiles, and cargo tracks it as an
  env-dep. `desktop-build.yml` sets `LIGHTBULB_API_BASE` for the whole `cargo` invocation, so
  core sees the same value. A plain Linux `cargo build` gets the prod default. A runtime
  override for self-hosters is **not** planned (decision 8).

**The plugin files compiled in from `packages/claude-plugin`:**
- The depth is preserved (§3), so no `include_str!` path changes. The Linux workflow must list
  `packages/claude-plugin/**` in `paths`. `sync-claude-plugin.yml` is unaffected.
- PR 8 edits two plugin skills (`/private/tmp` → `/tmp`). That ships to the public
  `lightbulb-plugin` mirror too, and changes nothing on a Mac.

**The shared fixtures:**
- `packages/shared/fixtures/local-session-frames/` is the wire contract between the Rust relay
  and the backend (`apps/backend/tests/unit/services/test_local_session_relay.py`). It does not
  move. Today a change there does not run TestDesktop. Both workflows add it to `paths` (PR 7,
  and PR 1 for Linux).
- `tests/fixtures/*.json` (holder terminal captures) move with `holder/model.rs` in PR 3.

**Other:**
- PR 6 conflicts with any open desktop PR. Use the freeze plus the rerunnable move script.
- About 25 items become `pub`. That is public API surface inside our own workspace only.
  Nothing is published to crates.io (decision 1).

---

## 7. What the open-source export contains afterwards

The export narrows from "all of `apps/desktop/`" to **the core crate, the plugin and the fixtures**:

| Path | Was | Now |
|---|---|---|
| `apps/desktop/core/` (src, tests, tests/fixtures, examples, Cargo.toml) | — | **public** |
| `apps/desktop/src-tauri/` (the app: window, agent Chrome, screen, node host, updater, terminal panel, icons, capabilities, sidecar scripts) | public | **private** |
| `packages/claude-plugin/` | public (already MIT) | public |
| `packages/shared/fixtures/local-session-frames/` | public | public |
| A public `Cargo.lock` | the app's | a core-only lock, made at sync time from `apps/desktop/Cargo.lock` with `cargo update --workspace` in a core-only workspace, then checked with `--locked`. **Not verified yet.** |

What this changes in `docs/research/open-source-client/` (a short update note is added at the top
of its README; the folder is otherwise not rewritten):
- The manifest shrinks from 172 files to about 90: about 60 core files (sources, 4 integration
  tests, 2 fixtures, the probe example, `Cargo.toml`, lock), 18 plugin files and 11 frames. Icons, `tauri.conf.json`, capabilities, `xterm.js`, entitlements, the sidecar scripts and
  `SIGNING.md` all leave the public scope.
- Three audit items stop applying to the **public** scope. They are still internal work:
  "Fix first" #1 (the IPC capability, done by #4283), #2 (XSS on the hub reaches IPC) and #5
  (the updater transport).
- §6 "Building against our hub" becomes `cargo build -p lightbulb-core` on a Mac or Linux. No
  Tauri, Xcode, signing, Node or sidecars are needed. The bundle-id collision (§6 #2) remains in
  smaller form: `config::APP_ID` names the Keychain service, the data dir, `serve.lock` and
  `~/.local/bin/lightbulb`.
- **The pitch is no longer true as written.** "Everything that runs on your computer is open" is
  false once the app is private. Something like "The session network client and CLI are open; the
  desktop app and the hub are hosted/private" is accurate (decision 7).
- `drafts/sync-desktop-client.yml` must rsync `apps/desktop/core/` (not `apps/desktop/`), drop the
  `SIGNING.md`/`bin/` excludes, and add the lockfile step. The gitleaks allowlist paths change from
  `apps/desktop/src-tauri/src/local_sessions.rs` to `apps/desktop/core/src/local_sessions.rs`. The S3
  bucket names (`session_pickup.rs:169-170`) and the redaction fakes move with core, so they stay public.
- Licence: Apache-2.0 for the core crate, as recommended before. Add `license` and `repository` to
  `core/Cargo.toml`.

---

## 8. Summary for the owner

We can split the session network and the CLI out of the desktop app into `lightbulb-core`
without changing what users see. About 40k of the 53.6k lines move. The session code
is already almost free of Tauri, so the work is five small decoupling PRs, then one rename-only
move, then a Linux port. The app keeps the window, agent Chrome, screen sharing, the Mac node,
the agent's file tools, the updater and the terminal panel. On a Mac nothing changes for users:
the app executable stays the `lightbulb` command and the `serve` service, so notifications,
signing and updates work as they do now. A standalone `lightbulb` binary builds without Tauri.
On Linux it needs one port PR before `serve` can find sessions, because today every
process lookup is macOS-only. Afterwards the open-source export is only the core crate, the
plugin and the protocol fixtures. That removes the app's IPC and updater questions from the
public scope, but the "everything on your computer is open" pitch has to change.

**Decisions only the owner can make:**
1. The name and place: `lightbulb-core` at `apps/desktop/core`, binary `lightbulb`, not published
   to crates.io.
2. Whether session pick-up and `lightbulb continue` go into core (recommended: `snapshot.rs`
   already depends on pick-up, and `continue` is a CLI command).
3. Whether the approved-folder allowlist (`file_access.json`) is core, with the agent's file tools
   private (recommended: resume and session start need it).
4. Linux key storage: a 0600 file under the data dir (recommended: it works headless, like SSH
   keys), or Secret Service (needs D-Bus and a session).
5. Linux service: `join` installs a systemd user unit, or `serve` runs in the foreground only.
6. macOS: no second binary in the bundle; the app executable stays the CLI (recommended).
7. The public pitch and licence (Apache-2.0) for the core-only export, and whether the app's
   source stays private for good.
8. Whether self-hosters get a runtime `LIGHTBULB_API_BASE` override (recommended: not now).
9. A short merge freeze on the desktop session files for PR 6.

## Owner decisions (2026-10-22)

All nine recommendations are approved as written in section 8:
1. `lightbulb-core` lives at `apps/desktop/core`. The binary is `lightbulb`. It is not published to crates.io.
2. Pick-up and `continue` go into core.
3. The approved-folder list goes into core; the agent's file tools stay private.
4. Linux keys are stored in a 0600 file.
5. Linux runs a systemd user unit from `join`.
6. On macOS the app executable stays the CLI.
7. Core is Apache-2.0; the app stays private.
8. No runtime API-base override for now.
9. A short merge freeze for PR 6.
