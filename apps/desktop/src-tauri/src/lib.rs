pub mod bootstrap;
pub mod claude_export;
pub mod device_token;
pub mod file_access;
pub mod keychain;
pub mod local_mcp;
pub mod local_sessions;
mod node_client;
mod node_invoke;
pub mod notify;
pub mod pair_client;
pub mod pair_state;
pub mod proxy;
pub mod screen;
mod desktop_approval;
pub mod subprocess;
mod tray;

use std::sync::{Arc, Mutex, RwLock};
use tauri::{Emitter, Manager, State, Url};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_updater::UpdaterExt;

use crate::bootstrap::{bootstrap_desktop_node_host, DesktopNodeHostHandles};
use crate::pair_client::DesktopPermissions;

/// Keychain service name for the desktop process. Matches the bundle ID so
/// macOS surfaces Keychain entries as belonging to this app.
const KEYCHAIN_SERVICE: &str = "company.thelightbulb.desktop";

/// HTTPS API base for the backend. Overridden at build time when a non-dev
/// build is produced.
const LIGHTBULB_API_BASE: &str = match option_env!("LIGHTBULB_API_BASE") {
    Some(v) => v,
    None => "https://api-dev.flickmeon.ai",
};

/// Shared auth token state
pub struct AuthState {
    pub token: Mutex<Option<String>>,
    /// Shared handle to the running node client's gateway URL. Updated whenever
    /// a fresh JWT arrives so the next reconnect uses current auth.
    ///
    /// Doubles as the "bootstrap completed" sentinel — only populated AFTER
    /// `run_bootstrap` returns Ok, or after the launch-time
    /// `start_node_host_from_persisted` path brings the node host up from
    /// persisted pair state. If bootstrap fails on the first call (a
    /// transient `/desktop/pair` 5xx, a Keychain permission prompt, or a
    /// missing sidecar binary), this stays `None` so the next
    /// `send_auth_token` retries bootstrap rather than dropping straight to
    /// the URL-rotation branch.
    pub gateway_url: Mutex<Option<Arc<RwLock<String>>>>,
    /// Current Clerk JWT, read by the proxy's JwtProvider closure on every
    /// upstream-connect. Updated in-place on rotation so the subprocess never
    /// has to restart.
    pub current_jwt: Arc<RwLock<String>>,
    /// True while a bootstrap is mid-flight. Set before spawning the
    /// bootstrap task and cleared in both branches (Ok and Err) of the
    /// spawned future. Prevents concurrent bootstrap attempts when a
    /// Clerk JWT refresh fires multiple `send_auth_token` calls in quick
    /// succession while the first attempt is still running.
    pub bootstrap_in_flight: Mutex<bool>,
    /// Loopback WSS proxy supervising the openclaw subprocess connection.
    /// `None` until the first `send_auth_token` triggers bootstrap.
    pub proxy_handle: Mutex<Option<crate::proxy::ProxyHandle>>,
    /// Bundled `openclaw node run` subprocess. Same lifetime as `proxy_handle`.
    pub subprocess_handle: Mutex<Option<crate::subprocess::SubprocessHandle>>,
    /// Serializes bootstrap and local-MCP mutation end to end: the
    /// `openclaw.json` read-modify-write AND sidecar startup/restart.
    ///
    /// Two reasons it covers both halves, not just the restart:
    /// - The restart is take-handle -> await shutdown -> await spawn, so two
    ///   concurrent callers would both see `None` in `subprocess_handle` and
    ///   leave a second, orphaned child attached to the proxy.
    /// - The config write is a read-modify-write, so two concurrent saves
    ///   would interleave and silently drop one update (last writer wins on a
    ///   value it read before the other's write).
    ///
    /// Async because the critical section spans awaits.
    pub node_host_mutation: tokio::sync::Mutex<()>,
    /// `/desktop/pair` response — retained so we can later call
    /// `/desktop/devices/{device_id}` (revoke) on logout.
    pub pair_response: Mutex<Option<crate::pair_client::PairResponse>>,
    /// Per-device gateway token from `/desktop/pair`. The node client reads
    /// this on every reconnect so the v3 signature payload binds to a
    /// non-empty token (upstream's signature verifier rejects mismatches).
    /// `None` until run_bootstrap completes.
    pub gateway_token: crate::node_client::GatewayTokenCell,
}

/// Node connection status
pub struct NodeState {
    pub status: Mutex<String>,
}

/// File-based logger for macOS GUI apps (stdout not visible).
pub fn log(msg: &str) {
    use std::io::Write;
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open("/tmp/lightbulb-desktop.log") {
        let _ = writeln!(f, "{}", msg);
    }
    println!("{}", msg);
}

/// Outcome of evaluating an incoming Clerk JWT against the current
/// bootstrap state. Factored out so the decision logic is unit-testable
/// without a Tauri AppHandle. See `decide_auth_action`.
#[derive(Debug, PartialEq, Eq)]
pub enum AuthAction {
    /// Bootstrap has previously succeeded — just rotate the gateway URL.
    Rotate,
    /// Bootstrap is in flight on another thread — drop this call (the
    /// running bootstrap will pick up the latest JWT via current_jwt).
    WaitForBootstrap,
    /// No prior successful bootstrap and no in-flight attempt — start
    /// bootstrap from this call.
    Bootstrap,
}

/// Pure decision function for `send_auth_token`. Inputs are the two
/// bits of mutable state that matter (without locking them inside the
/// function, so tests don't need to construct AuthState):
///
/// - `bootstrap_completed`: true if `gateway_url` is `Some(_)`
/// - `bootstrap_in_flight`: current value of the in-flight guard
///
/// The (`true`, `true`) case (bootstrap finished AND another attempt is
/// flagged in-flight) shouldn't happen in practice because the spawned
/// task clears `in_flight` BEFORE publishing `gateway_url`, but we
/// handle it conservatively by rotating — the most-recent bootstrap
/// completed, so its URL handle is the source of truth.
pub fn decide_auth_action(bootstrap_completed: bool, bootstrap_in_flight: bool) -> AuthAction {
    if bootstrap_completed {
        AuthAction::Rotate
    } else if bootstrap_in_flight {
        AuthAction::WaitForBootstrap
    } else {
        AuthAction::Bootstrap
    }
}

#[cfg(test)]
mod auth_action_tests {
    use super::*;

    #[test]
    fn first_call_kicks_off_bootstrap() {
        assert_eq!(decide_auth_action(false, false), AuthAction::Bootstrap);
    }

    #[test]
    fn second_call_after_bootstrap_failure_retries() {
        // Regression test: if bootstrap fails (transient /desktop/pair
        // error, Keychain permission denial, missing sidecar binary),
        // gateway_url MUST stay None so the next send_auth_token retries.
        // `bootstrap_completed` is keyed off the URL handle that is only
        // published after bootstrap succeeds — not off `token`, which is
        // set BEFORE the spawned bootstrap task and would otherwise cause
        // every retry to silently drop into the rotation branch without
        // bringing the desktop node back up.
        assert_eq!(decide_auth_action(false, false), AuthAction::Bootstrap);
    }

    #[test]
    fn call_during_in_flight_bootstrap_defers() {
        assert_eq!(decide_auth_action(false, true), AuthAction::WaitForBootstrap);
    }

    #[test]
    fn call_after_successful_bootstrap_rotates() {
        assert_eq!(decide_auth_action(true, false), AuthAction::Rotate);
    }

    #[test]
    fn rotate_wins_over_stale_in_flight_flag() {
        // Defense-in-depth: if a stale `in_flight=true` somehow
        // coexists with `bootstrap_completed=true`, prefer the
        // rotation path — the URL handle exists, so the system is
        // already up. We don't want to silently drop tokens.
        assert_eq!(decide_auth_action(true, true), AuthAction::Rotate);
    }
}

/// Called by the web app to send the Clerk JWT to the Rust backend.
/// Starts the node-host connection directly to the gateway.
///
/// Idempotent in two stages:
///
///   1. Subsequent calls AFTER bootstrap has succeeded just rotate the
///      shared `gateway_url` so the next reconnect picks up the fresh
///      token. The proxy + subprocess keep running.
///   2. Subsequent calls AFTER bootstrap has FAILED retry bootstrap —
///      this would otherwise silently fall through to the rotation
///      branch because the `token` was already set, even though the
///      proxy/subprocess never came up.
#[tauri::command]
fn send_auth_token(
    token: String,
    display_name: String,
    user_id: String,
    permissions: Option<DesktopPermissions>,
    state: State<'_, AuthState>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    {
        // Cache the latest token for telemetry / diagnostic dumps. We do
        // NOT use this as the "did bootstrap already happen" sentinel
        // anymore — see the gateway_url check below.
        let mut t = state.token.lock().map_err(|e| e.to_string())?;
        *t = Some(token.clone());
    }
    log(&format!("[auth] Received JWT ({} chars) for user {} ({})", token.len(), display_name, user_id));

    // Update the shared JWT cell FIRST so the proxy's JwtProvider closure
    // picks up the fresh token on the next upstream-connect. We do this
    // unconditionally — even mid-bootstrap, a fresher token is better than
    // the one we started bootstrap with.
    if let Ok(mut current) = state.current_jwt.write() {
        *current = token.clone();
    }

    let ws_url = option_env!("LIGHTBULB_WS_URL").unwrap_or("wss://bv-dev.flickmeon.ai/api/v1/gateway/ws");
    // Use an explicit "/" path before the query. tokio-tungstenite builds
    // the HTTP upgrade request line from the URL's path+query; if the URL
    // is `wss://host?token=…` (no slash), the resulting request line is
    // malformed and AWS ELB rejects with HTTP 400 before API Gateway ever
    // sees it. Browsers normalize this implicitly — native WS clients do
    // not.
    // clientId picks the verification branch in routers/gateway_relay.py's
    // `/ws` handler -> machine_auth.verify_upgrade: "openclaw-macos" is in
    // NODE_CLIENT_IDS, so the relay checks this JWT as a paired-device EdDSA
    // credential rather than a Paperclip operator's HS256 service token. The
    // real challenge is the upstream container's challenge, signed by
    // Tauri-mac locally with the Mac-slot Keychain key in
    // node_client::perform_handshake.
    let new_gateway_url = format!("{}/?token={}&clientId=openclaw-macos", ws_url, token);

    // Decide what to do with this token. See `AuthAction` for the cases.
    let existing_handle: Option<Arc<RwLock<String>>> = state
        .gateway_url
        .lock()
        .ok()
        .and_then(|g| g.as_ref().cloned());
    let in_flight_already = state
        .bootstrap_in_flight
        .lock()
        .map(|g| *g)
        .unwrap_or(false);
    match decide_auth_action(existing_handle.is_some(), in_flight_already) {
        AuthAction::Rotate => {
            // Rotation path — bootstrap already succeeded once. Update the
            // shared URL string in place; the node client will pick it up
            // on its next reconnect.
            if let Some(handle) = existing_handle {
                if let Ok(mut url) = handle.write() {
                    *url = new_gateway_url;
                    log("[auth] Rotated node client URL with fresh token");
                }
            }
            return Ok(());
        }
        AuthAction::WaitForBootstrap => {
            log("[auth] bootstrap already in flight; deferring to existing attempt");
            return Ok(());
        }
        AuthAction::Bootstrap => {
            // Mark in-flight under the lock so a near-simultaneous second
            // call sees the flag set and falls into WaitForBootstrap.
            if let Ok(mut in_flight) = state.bootstrap_in_flight.lock() {
                *in_flight = true;
            }
        }
    }

    let app_handle = app.clone();
    let jwt_arc = state.current_jwt.clone();
    let display_name_for_chain = display_name.clone();
    let permissions_for_chain = permissions.clone();
    tauri::async_runtime::spawn(async move {
        let result = run_bootstrap(
            &app_handle,
            jwt_arc,
            &display_name_for_chain,
            permissions_for_chain,
        )
        .await;

        // Clear the in-flight flag regardless of outcome BEFORE deciding
        // whether to publish gateway_url. If we clear it after publishing
        // and a token arrives in the gap, we'd see both flags unset and
        // could kick off a second bootstrap.
        if let Some(state) = app_handle.try_state::<AuthState>() {
            if let Ok(mut in_flight) = state.bootstrap_in_flight.lock() {
                *in_flight = false;
            }
        }

        match result {
            Err(e) => {
                log(&format!(
                    "[bootstrap] desktop node-host bootstrap failed (will retry on next send_auth_token): {}",
                    e
                ));
                update_node_status(&app_handle, "error");
                // Intentionally do NOT set gateway_url here — the next
                // send_auth_token call must re-enter the bootstrap path.
            }
            Ok(()) => {
                // Bootstrap succeeded. Publish gateway_url AFTER pair +
                // proxy + subprocess are all live, so the rotation
                // branch above can only see a URL handle that's actually
                // backed by a working proxy/subprocess pair.
                let shared_url = Arc::new(RwLock::new(new_gateway_url));
                if let Some(state) = app_handle.try_state::<AuthState>() {
                    if let Ok(mut handle_guard) = state.gateway_url.lock() {
                        *handle_guard = Some(shared_url.clone());
                    }
                }
                if let Err(e) = start_node_host(&app_handle, shared_url, &display_name).await {
                    log(&format!("[node] Failed to start: {}", e));
                    update_node_status(&app_handle, "error");
                }
            }
        }
    });

    Ok(())
}

/// Resolve Tauri-derived paths, run the bootstrap chain, and stash the
/// returned handles on `AuthState`.
///
/// Split out so the call site in `send_auth_token` stays readable and so
/// the AppHandle-bound work is isolated from the pure bootstrap logic
/// (which lives in `bootstrap::bootstrap_desktop_node_host` and has its
/// own unit tests).
async fn run_bootstrap(
    app: &tauri::AppHandle,
    jwt_arc: Arc<RwLock<String>>,
    display_name: &str,
    permissions: Option<DesktopPermissions>,
) -> Result<(), String> {
    let state = app
        .try_state::<AuthState>()
        .ok_or_else(|| "AuthState not managed".to_string())?;
    // Startup also rewrites the private config; keep MCP edits/restarts out
    // until the new subprocess handles have been published.
    let _serialized = state.node_host_mutation.lock().await;
    let jwt_snapshot = jwt_arc
        .read()
        .map(|g| g.clone())
        .map_err(|e| format!("jwt read: {e}"))?;
    if jwt_snapshot.is_empty() {
        return Err("bootstrap called with empty JWT".into());
    }

    // The Tauri path resolver lives behind `Manager::path()` in Tauri 2.
    // `app_data_dir` is the per-app data dir (macOS:
    // ~/Library/Application Support/company.thelightbulb.desktop); `resource_dir` is
    // the bundle's Resources/ directory in release builds.
    let app_data = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app_data_dir: {e}"))?;
    let state_dir = state_dir_in(&app_data);

    let binary = resolve_node_host_binary()?;

    log(&format!(
        "[bootstrap] api_base={} state_dir={:?} binary={:?}",
        LIGHTBULB_API_BASE, state_dir, binary
    ));

    let handles: DesktopNodeHostHandles = bootstrap_desktop_node_host(
        LIGHTBULB_API_BASE,
        &jwt_snapshot,
        KEYCHAIN_SERVICE,
        "node-host".to_string(),
        state_dir,
        binary,
        permissions,
    )
    .await?;

    log(&format!(
        "[bootstrap] paired device_id_mac={} device_id_cli={} proxy_port={}",
        handles.pair_response.device_id_mac,
        handles.pair_response.device_id_cli,
        handles.proxy_handle.local_port()
    ));

    // Persist the pair essentials so the NEXT app launch can start the node
    // host without waiting for the webview to deliver a Clerk JWT (see
    // start_node_host_from_persisted). Non-fatal: a failed write only means
    // no standalone start next launch — this session is unaffected.
    if let Err(e) = pair_state::save(
        &app_data,
        &pair_state::PairState::from_pair_response(&handles.pair_response, display_name),
    ) {
        log(&format!(
            "[bootstrap] failed to persist pair state (standalone relaunch start disabled): {e}"
        ));
    }

    // Pairing can complete after the webview already reported (or never
    // reported) a signed-in user; a paused/never-started Controller waits on
    // the retained pair `restore` needs, so re-run it now that pair state is
    // on disk instead of waiting for the next unrelated trigger.
    let controller = app.state::<local_sessions::enrollment::Controller>();
    if let Err(e) = controller.restore(app).await {
        log(&format!("[local-sessions] restore after pairing failed: {e}"));
    }

    stash_handles(app, handles);

    Ok(())
}

/// `OPENCLAW_STATE_DIR` for the sidecar: `<app_data_dir>/openclaw-state`.
/// Single definition so the config file `local_mcp` writes and the state
/// dir `subprocess::spawn` exports can never drift apart.
fn state_dir_in(app_data: &std::path::Path) -> std::path::PathBuf {
    app_data.join("openclaw-state")
}

/// Same, resolved from an AppHandle.
fn openclaw_state_dir(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    app.path()
        .app_data_dir()
        .map(|d| state_dir_in(&d))
        .map_err(|e| format!("app_data_dir: {e}"))
}

/// Stop the openclaw sidecar and respawn it against the EXISTING proxy.
///
/// The node host reads `nodeHost.mcp.servers` once, at startup
/// (`getRuntimeConfig()`), and nothing watches the file on that path — so a
/// config write is inert until the process is bounced.
///
/// Deliberately NOT a replay of `bootstrap::start_from_pair`: that function
/// also calls `start_proxy`, so re-entering it would bind a second loopback
/// listener and leak the first (its `ProxyHandle` — the only thing holding
/// the shutdown channel — would be dropped when we overwrote the slot, but
/// the accept loop and any in-flight upstream connections belong to a task
/// we no longer signal). The proxy is also the wrong thing to restart: it
/// holds the device-token minter and re-mints on every upstream open, so it
/// survives a child bounce untouched and the child reconnects through it on
/// the same port. Everything else `spawn` needs — identity file, retired
/// `node.json` sweep — is idempotent by construction.
///
/// A missing proxy means the node host was never started this session, so
/// there is nothing to bounce and the next launch picks the config up.
/// Caller MUST already hold `AuthState::node_host_mutation`. The lock covers the
/// config read-modify-write as well as the restart — see `local_mcp_set`.
async fn restart_node_host_subprocess_locked(app: &tauri::AppHandle) -> Result<(), String> {
    let state = app
        .try_state::<AuthState>()
        .ok_or_else(|| "AuthState not managed".to_string())?;

    let proxy_port = state
        .proxy_handle
        .lock()
        .map_err(|e| format!("proxy_handle lock: {e}"))?
        .as_ref()
        .map(|p| p.local_port());
    let Some(proxy_port) = proxy_port else {
        log("[local-mcp] node host not running; config takes effect on next launch");
        return Ok(());
    };

    let gateway_token = state
        .gateway_token
        .read()
        .map_err(|e| format!("gateway_token lock: {e}"))?
        .clone()
        .ok_or_else(|| "no gateway token yet; pair the device first".to_string())?;

    // Take the handle BEFORE awaiting so a caller that races past the lock
    // (impossible today, but cheap) can't shut the same child down twice.
    let previous = state
        .subprocess_handle
        .lock()
        .map_err(|e| format!("subprocess_handle lock: {e}"))?
        .take();
    if let Some(previous) = previous {
        previous.shutdown().await;
    }

    let cli_id = keychain::load_or_generate(keychain::KeychainSlot::Cli, KEYCHAIN_SERVICE)?;
    // The previous child is already gone by this point, so a spawn failure
    // leaves the node host DOWN — not just local MCP, but chat and the whole
    // gateway surface for this session, until the app is relaunched. Say that,
    // rather than returning a bare spawn error the settings pane would render
    // as a generic "save failed" next to a config that did in fact save.
    //
    // Deliberately no retry: the realistic failures here are deterministic
    // (missing/unresolvable sidecar binary, bad state dir), so a retry would
    // just double the wait before the same message.
    let handle = match subprocess::spawn(
        resolve_node_host_binary()?,
        proxy_port,
        openclaw_state_dir(app)?,
        &cli_id.private_key_pem,
        &cli_id.public_key_pem,
        &gateway_token,
    )
    .await
    {
        Ok(handle) => handle,
        Err(e) => {
            log(&format!(
                "[local-mcp] sidecar respawn FAILED after teardown; node host is down: {e}"
            ));
            return Err(format!(
                "Your MCP settings were saved, but the local agent host could not be \
                 restarted and is now stopped. Quit and reopen Lightbulb to bring it \
                 back. ({e})"
            ));
        }
    };
    *state
        .subprocess_handle
        .lock()
        .map_err(|e| format!("subprocess_handle lock: {e}"))? = Some(handle);
    log(&format!(
        "[local-mcp] node host sidecar restarted on proxy port {proxy_port}"
    ));
    Ok(())
}

/// The `nodeHost.mcp.servers` object from the Mac-side openclaw config.
/// `{}` when nothing is configured yet.
#[tauri::command]
async fn local_mcp_list(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    local_mcp::list_servers(&openclaw_state_dir(&app)?)
}

#[tauri::command]
async fn local_browser_status(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    local_mcp::browser_setup_status(&openclaw_state_dir(&app)?)
}

#[tauri::command]
async fn local_browser_configure(app: tauri::AppHandle, extension: bool) -> Result<(), String> {
    let state = app.try_state::<AuthState>().ok_or("AuthState not managed")?;
    let _serialized = state.node_host_mutation.lock().await;
    local_mcp::configure_browser(&openclaw_state_dir(&app)?, extension)?;
    restart_node_host_subprocess_locked(&app).await
}

#[tauri::command]
async fn local_browser_pairing(app: tauri::AppHandle) -> Result<String, String> {
    local_mcp::browser_pairing(&openclaw_state_dir(&app)?)
}

/// Replace the whole servers map, then bounce the sidecar so it takes effect.
///
/// The lock spans the read-modify-write AND the restart. Guarding only the
/// restart would let two concurrent calls interleave their RMW of
/// `openclaw.json` and silently drop one update — last writer wins on a value
/// it read before the other's write.
#[tauri::command]
async fn local_mcp_set(app: tauri::AppHandle, servers: serde_json::Value) -> Result<(), String> {
    let state = app
        .try_state::<AuthState>()
        .ok_or_else(|| "AuthState not managed".to_string())?;
    let _serialized = state.node_host_mutation.lock().await;
    local_mcp::set_servers(&openclaw_state_dir(&app)?, servers)?;
    restart_node_host_subprocess_locked(&app).await
}

/// Remove one server. `false` (and no restart) if it wasn't there.
#[tauri::command]
async fn local_mcp_remove(app: tauri::AppHandle, name: String) -> Result<bool, String> {
    let state = app
        .try_state::<AuthState>()
        .ok_or_else(|| "AuthState not managed".to_string())?;
    let _serialized = state.node_host_mutation.lock().await;
    if !local_mcp::remove_server(&openclaw_state_dir(&app)?, &name)? {
        return Ok(false);
    }
    restart_node_host_subprocess_locked(&app).await?;
    Ok(true)
}

/// Resolve the launcher shim registered as `bin/lightbulb-browser-service` in
/// `tauri.conf.json` (externalBin). Tauri's bundler finds
/// `bin/lightbulb-browser-service-<target-triple>` at build time, strips the
/// triple, and places the file in the `.app` at
/// `Contents/MacOS/lightbulb-browser-service` — alongside the main Tauri
/// executable. Dev builds replicate the layout next to
/// `target/debug/<exe>`. So we resolve via current_exe().parent().
///
/// `lightbulb-browser-service` reads `LIGHTBULB_PROXY_PORT` and dials loopback
/// plaintext WS — what `spawn_subprocess` is designed to talk to.
fn resolve_node_host_binary() -> Result<std::path::PathBuf, String> {
    let exe = std::env::current_exe().map_err(|e| format!("current_exe: {e}"))?;
    let parent = exe
        .parent()
        .ok_or_else(|| "current_exe has no parent".to_string())?;
    let binary = parent.join("lightbulb-browser-service");
    if !binary.exists() {
        return Err(format!(
            "lightbulb-browser-service shim not found at {}",
            binary.display()
        ));
    }
    Ok(binary)
}

/// Stash bootstrap handles on `AuthState` so the proxy and subprocess
/// outlive the task that created them. We grab the state lazily because
/// there's no `State<'_, AuthState>` in the async contexts that call this.
fn stash_handles(app: &tauri::AppHandle, handles: DesktopNodeHostHandles) {
    if let Some(state) = app.try_state::<AuthState>() {
        // Publish gateway_token BEFORE storing pair_response (we move it next).
        // start_node_host snapshots gateway_token on each reconnect, so the
        // first connect needs to see it already populated.
        if let Ok(mut g) = state.gateway_token.write() {
            *g = Some(handles.pair_response.gateway_token.clone());
        }
        if let Ok(mut g) = state.proxy_handle.lock() {
            *g = Some(handles.proxy_handle);
        }
        if let Ok(mut g) = state.subprocess_handle.lock() {
            *g = Some(handles.subprocess_handle);
        }
        if let Ok(mut g) = state.pair_response.lock() {
            *g = Some(handles.pair_response);
        }
    }
}

/// Launch-time standalone start: if a previous `/pair` persisted its state
/// and the Keychain still holds the matching device keys, bring up the FULL
/// desktop-node surface (loopback proxy + openclaw subprocess + Mac node
/// client) immediately — no webview, no Clerk. Every credential involved is
/// self-minted from the Keychain keys per connect attempt.
///
/// The webview Clerk path (`send_auth_token` → bootstrap) remains the ONLY
/// pairing path: it runs the FIRST pair (no persisted state yet) and the
/// re-pair after persisted state goes stale (version mismatch makes `load`
/// return `None`; Keychain wipe causes a device-id mismatch discarded here).
///
/// Returns `Ok(true)` if the node host was started from persisted state,
/// `Ok(false)` if there was nothing (valid) persisted — the normal first-run
/// case, NOT an error.
async fn start_node_host_from_persisted(app: &tauri::AppHandle) -> Result<bool, String> {
    let app_data = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app_data_dir: {e}"))?;
    let Some(persisted) = pair_state::load(&app_data) else {
        return Ok(false);
    };

    // The Keychain is the source of truth for identity. If its entries were
    // wiped, load_or_generate just minted FRESH keys whose derived device
    // ids can't match what we paired with — discard the stale state and let
    // the webview /pair path re-pair with the new keys.
    let mac_id = keychain::load_or_generate(keychain::KeychainSlot::Mac, KEYCHAIN_SERVICE)?;
    let cli_id = keychain::load_or_generate(keychain::KeychainSlot::Cli, KEYCHAIN_SERVICE)?;
    if !persisted.matches_identities(&mac_id.public_key_pem, &cli_id.public_key_pem) {
        log("[setup] persisted pair state does not match Keychain identities; discarding");
        pair_state::clear(&app_data);
        return Ok(false);
    }

    // Claim the bootstrap slot so a webview JWT arriving mid-start defers to
    // us (AuthAction::WaitForBootstrap) instead of double-bootstrapping.
    {
        let state = app
            .try_state::<AuthState>()
            .ok_or_else(|| "AuthState not managed".to_string())?;
        let mut in_flight = state
            .bootstrap_in_flight
            .lock()
            .map_err(|e| e.to_string())?;
        if *in_flight {
            // A webview bootstrap beat us to it; defer to that attempt.
            return Ok(false);
        }
        *in_flight = true;
    }

    let result = start_from_persisted_state(app, persisted, cli_id, app_data).await;

    // Clear the in-flight flag on BOTH outcomes — a stuck flag would
    // dead-end every future send_auth_token in WaitForBootstrap. (Cleared
    // after gateway_url publication; the transient completed+in-flight
    // window resolves to Rotate, per decide_auth_action.)
    if let Some(state) = app.try_state::<AuthState>() {
        if let Ok(mut in_flight) = state.bootstrap_in_flight.lock() {
            *in_flight = false;
        }
    }

    result.map(|()| true)
}

/// The persisted-state twin of `run_bootstrap`'s success path: replay the
/// post-pair bootstrap leg, stash handles, publish `gateway_url`, start the
/// Mac node client.
async fn start_from_persisted_state(
    app: &tauri::AppHandle,
    persisted: pair_state::PairState,
    cli_id: keychain::DeviceIdentity,
    app_data: std::path::PathBuf,
) -> Result<(), String> {
    let state = app
        .try_state::<AuthState>()
        .ok_or_else(|| "AuthState not managed".to_string())?;
    let _serialized = state.node_host_mutation.lock().await;
    let state_dir = state_dir_in(&app_data);
    let binary = resolve_node_host_binary()?;
    let display_name = persisted.display_name.clone();

    log(&format!(
        "[setup] starting node host from persisted pair state (device_id_mac={} device_id_cli={})",
        persisted.device_id_mac, persisted.device_id_cli
    ));

    let handles = bootstrap::start_from_pair(
        &cli_id,
        persisted.to_pair_response(),
        "node-host".to_string(),
        state_dir,
        binary,
    )
    .await?;

    stash_handles(app, handles);

    // Publish the gateway URL with NO token baked in — the node client's
    // per-attempt token provider self-mints the $connect token, so nothing
    // on this path ever depends on a webview JWT. Publishing also flips
    // send_auth_token into its rotate-only branch, so a later webview JWT
    // just refreshes the URL cell instead of re-bootstrapping.
    let ws_url = option_env!("LIGHTBULB_WS_URL").unwrap_or("wss://bv-dev.flickmeon.ai/api/v1/gateway/ws");
    let shared_url = Arc::new(RwLock::new(format!(
        "{}/?clientId=openclaw-macos",
        ws_url
    )));
    if let Some(state) = app.try_state::<AuthState>() {
        if let Ok(mut handle_guard) = state.gateway_url.lock() {
            *handle_guard = Some(shared_url.clone());
        }
    }

    start_node_host(app, shared_url, &display_name)
        .await
        .map_err(|e| e.to_string())
}

async fn start_node_host(
    app: &tauri::AppHandle,
    gateway_url: Arc<RwLock<String>>,
    display_name: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    update_node_status(app, "connecting");
    log("[node] Starting node client");

    // Load (or generate, if missing) the Mac-slot Ed25519 identity. The
    // bootstrap chain above already created+persisted this key into the
    // Keychain, so this call is effectively a load with the generate path
    // as a defensive fallback if Keychain was wiped between bootstrap
    // and start_node_host.
    let identity = keychain::load_or_generate(keychain::KeychainSlot::Mac, KEYCHAIN_SERVICE)
        .map_err(|e| format!("load Mac identity: {e}"))?;

    // Snapshot the gateway-token cell from AuthState. The node client owns
    // the Arc and re-reads it on each reconnect — that's how a future
    // revoke+re-pair flow could rotate the token without restart.
    let gateway_token = app
        .try_state::<AuthState>()
        .map(|s| s.gateway_token.clone())
        .unwrap_or_else(|| Arc::new(RwLock::new(None)));

    // Self-mint the $connect token from our own Keychain key, so the node
    // never depends on the webview having pushed a Clerk JWT.
    //
    // The URL cell carries a ~60s Clerk JWT that ONLY `send_auth_token`
    // refreshes. When the webview does not run that push the node reconnects
    // forever against a dead token and 403s with no recovery — measured on a
    // live install 2026-08-05: 66 consecutive `403 Forbidden` against 0 token
    // pushes, at normal scheduling priority (so not App Nap). This is the same
    // failure #1181 fixed for the CLI node-host via `proxy::JwtProvider`; the
    // Mac node never got it.
    //
    // Safe because the WS authorizer resolves ANY paired device by id
    // (`paired_devices[deviceId]`, websocket-authorizer/index.py:236) — not
    // just the CLI one — and the Mac device is registered there with
    // clientId=openclaw-macos.
    //
    // No webview-token fallback: both callers (post-/pair bootstrap and the
    // persisted-state launch path) guarantee pair_response is populated, and
    // a $connect on a webview JWT recreates the 403 loop this provider
    // exists to kill — so a missing pair response is a hard error.
    let (device_id_mac, owner_id) = app
        .try_state::<AuthState>()
        .and_then(|s| {
            s.pair_response.lock().ok().and_then(|g| {
                g.as_ref()
                    .map(|p| (p.device_id_mac.clone(), p.owner_id.clone()))
            })
        })
        .ok_or("no pair response — node host cannot self-authenticate")?;

    let mut client = node_client::NodeClient::with_shared_url(
        gateway_url,
        display_name,
        identity.clone(),
        gateway_token,
    );

    let key_pem = identity.private_key_pem.clone();
    client.set_token_provider(Arc::new(move || {
        // 90s lifetime matches the CLI node-host (#1181): long enough
        // to absorb clock skew, short enough that a leaked token is
        // near-worthless.
        match device_token::mint_device_token(&key_pem, &device_id_mac, &owner_id, 90) {
            Ok(t) => t,
            Err(e) => {
                log(&format!("[node] device-token mint failed: {e}"));
                String::new()
            }
        }
    }));
    log("[node] using self-minted device token for $connect");

    // Flip the tray/renderer status ONLY when the node client emits a real
    // transition. Previously we said "connected" right after start() returned,
    // but start() just spawns the connection loop — the WS handshake hadn't
    // happened yet. A bad token or network hiccup left the UI claiming
    // connected while the node was actually still dialing.
    let app_for_cb = app.clone();
    client.on_status_change(move |status| {
        update_node_status(&app_for_cb, status);
    });

    let invoke_rx = client.start().await?;
    log("[node] Node client started, listening for invoke requests");

    let app_for_invoke = app.clone();
    tokio::spawn(async move {
        node_invoke::run(client, app_for_invoke, invoke_rx).await;
        log("[node] Invoke receiver closed");
    });

    Ok(())
}

fn update_node_status(app: &tauri::AppHandle, status: &str) {
    log(&format!("[node-status] transition -> {}", status));
    let label = match status {
        "connecting" => "Node: Connecting...",
        "connected" => "Node: Connected",
        "error" => "Node: Error",
        _ => "Ready",
    };
    tray::update_tray_status(app, label);
    let _ = app.emit("node:status", status);

    if let Some(state) = app.try_state::<NodeState>() {
        *state.status.lock().unwrap() = status.into();
    }
}

/// Returns true — lets the web app detect it's running in the desktop app.
#[tauri::command]
fn is_desktop() -> bool {
    true
}

/// Returns the current node connection status.
#[tauri::command]
fn get_node_status(state: State<'_, NodeState>) -> String {
    state.status.lock().unwrap().clone()
}

// ============================================================================
// File-access allowlist Tauri commands (consumed by the Settings UI).
// ============================================================================

/// Returns the current list of canonical paths the agent is allowed to
/// read/write through file.* and dir.* commands.
#[tauri::command]
fn file_access_list_paths() -> Vec<String> {
    file_access::list_paths()
}

/// Adds `path` to the allowlist after canonicalizing it. Returns the
/// canonical form. Idempotent. Errors if the path doesn't exist or
/// isn't accessible.
#[tauri::command]
fn file_access_add_path(path: String) -> Result<String, String> {
    file_access::add_path(&path).map_err(|e| e.to_string())
}

/// Removes `path` from the allowlist (canonical match). Returns true if
/// something was removed.
#[tauri::command]
fn file_access_remove_path(path: String) -> Result<bool, String> {
    file_access::remove_path(&path).map_err(|e| e.to_string())
}

/// Convenience for the Settings UI to show the auto-seed path even
/// before a user has added anything custom.
#[tauri::command]
fn file_access_default_path() -> Result<String, String> {
    file_access::default_seed_path()
        .map(|p| p.to_string_lossy().to_string())
        .map_err(|e| e.to_string())
}

/// Identity tuple the webview needs to construct the manifest URL:
/// `(target, current_version)` matching the path pattern in
/// `apps/frontend/src/app/api/desktop/update/[target]/[current_version]/route.ts`.
///
/// We compute these in Rust rather than asking the webview to derive
/// them because:
///   - `target` is environment-specific (`darwin-aarch64` vs
///     `darwin-x86_64`); Tauri exposes it via `updater::target()` at
///     build time and the webview has no clean way to read it.
///   - `current_version` lives in `tauri.conf.json` and is bumped by
///     the release workflow; the most reliable way to read it is
///     `app.package_info().version`.
#[derive(serde::Serialize)]
struct UpdaterIdentity {
    target: String,
    current_version: String,
}

#[tauri::command]
fn updater_identity(app: tauri::AppHandle) -> UpdaterIdentity {
    // Build target as "<os>-<arch>" to match the path pattern. Tauri's
    // `updater::target()` helper produces the canonical string the
    // plugin uses for endpoint substitution (`darwin-aarch64`,
    // `darwin-x86_64`, `linux-x86_64`, etc.).
    let target = tauri_plugin_updater::target().unwrap_or_else(|| "unknown".to_string());
    let current_version = app.package_info().version.to_string();
    UpdaterIdentity {
        target,
        current_version,
    }
}

/// Updater manifest payload passed in from the webview.
///
/// This is the same shape that `apps/frontend/src/app/api/desktop/update/...`
/// returns — and identically the shape that `tauri-plugin-updater` parses
/// as a `Dynamic` `RemoteRelease`. The webview fetches the manifest
/// (carrying its Vercel SSO cookie) and hands it to us so the Rust side
/// never has to fight Vercel's deployment protection with its own
/// cookie-less reqwest client.
///
/// `url` is a github.com release-asset URL (PUBLIC, no auth needed), so
/// the actual bundle download from Rust is unauthenticated.
///
/// `signature` is a base64 minisign signature, verified against the
/// pubkey baked into tauri.conf.json by `Update::download` before any
/// bytes hit disk.
#[derive(Debug, serde::Deserialize)]
struct UpdateManifest {
    version: String,
    url: String,
    signature: String,
    #[serde(default)]
    notes: Option<String>,
    #[serde(default)]
    pub_date: Option<String>,
}

/// Apply an update manifest fetched from the webview.
///
/// Flow:
///   1. Spawn an ephemeral loopback HTTP server that serves the manifest
///      as the plugin's `Dynamic` `RemoteRelease` JSON.
///   2. Build a per-call `Updater` pointed at the loopback endpoint via
///      `UpdaterBuilder::endpoints`. This is the only way to feed a
///      hand-built manifest through the plugin's public API — the
///      `Update` struct has crate-private fields and can't be
///      constructed directly.
///   3. `updater.check().await` parses the manifest, fetches it through
///      the plugin's own version comparator, and returns the same
///      `Update` object you'd get from the upstream endpoint.
///   4. `download_and_install` fetches the github.com bundle (no auth
///      needed — public release asset), verifies its minisign signature
///      against the pubkey from tauri.conf.json, swaps the .app, and
///      we call `app.restart()`.
///
/// The signature verification path is unchanged from the previous
/// design — Tauri's `verify_signature` still runs. We've only moved
/// the metadata-fetch step out of reqwest and into the webview, which
/// IS Vercel-authenticated.
///
/// NOTE: the loopback endpoint is `http://127.0.0.1:<port>`, which
/// `UpdaterBuilder::endpoints` rejects in release builds unless
/// `dangerousInsecureTransportProtocol: true` is set in
/// `tauri.conf.json` (`plugins.updater`). That flag is safe HERE and
/// only here: it relaxes the transport check for the *manifest* fetch,
/// which is a same-machine loopback serving JSON the webview already
/// pulled over HTTPS. The bundle download URL inside that manifest is
/// still an HTTPS release-asset URL, and its minisign signature is
/// still verified against the baked-in pubkey before install — the
/// real security boundary is untouched. Without the flag, every
/// in-app update fails at `set endpoints` with "must use a secure
/// protocol like `https`".
#[tauri::command]
async fn apply_update_manifest(
    app: tauri::AppHandle,
    manifest: UpdateManifest,
) -> Result<(), String> {
    log(&format!(
        "[updater] applying webview manifest: version={} url={}",
        manifest.version, manifest.url
    ));

    // The plugin parses `Dynamic` RemoteRelease as:
    //   { "version": "...", "notes": "...", "pub_date": "...",
    //     "url": "...", "signature": "..." }
    // — which is exactly what our Next.js route returns. Serialize
    // back to JSON to feed the loopback server.
    let manifest_json = serde_json::json!({
        "version": manifest.version,
        "notes": manifest.notes,
        "pub_date": manifest.pub_date,
        "url": manifest.url,
        "signature": manifest.signature,
    })
    .to_string();

    let endpoint = serve_manifest_once(manifest_json)
        .await
        .map_err(|e| {
            log(&format!("[updater] loopback manifest server failed: {}", e));
            format!("loopback manifest server: {e}")
        })?;
    log(&format!("[updater] loopback manifest endpoint: {}", endpoint));

    let endpoint_url = Url::parse(&endpoint).map_err(|e| {
        log(&format!("[updater] loopback url parse failed: {}", e));
        format!("loopback url parse: {e}")
    })?;

    let updater = app
        .updater_builder()
        .endpoints(vec![endpoint_url])
        .map_err(|e| {
            log(&format!("[updater] set endpoints failed: {}", e));
            format!("set endpoints: {e}")
        })?
        // The plugin's default comparator does `release.version > current_version`.
        // Our backend route already gated on `isNewer(latestVersion, currentVersion)`
        // (returning 204 otherwise), so by the time we get here the webview has
        // already confirmed there IS a newer version. Force-accept the comparison
        // so any tag-format mismatch can't drop us into a silent no-op.
        .version_comparator(|_current, _release| true)
        .build()
        .map_err(|e| {
            log(&format!("[updater] build updater failed: {}", e));
            format!("build updater: {e}")
        })?;

    let update = updater
        .check()
        .await
        .map_err(|e| {
            log(&format!("[updater] check failed: {}", e));
            format!("updater check: {e}")
        })?
        .ok_or_else(|| {
            log("[updater] check returned None for webview-supplied manifest");
            "updater returned no update for webview-supplied manifest".to_string()
        })?;

    log(&format!(
        "[updater] installing {} -> {}",
        update.current_version, update.version
    ));

    update
        .download_and_install(
            |_chunk, _total| {},
            || log("[updater] download finished, installing"),
        )
        .await
        .map_err(|e| {
            log(&format!("[updater] install failed: {}", e));
            format!("install: {e}")
        })?;

    log("[updater] install complete, restarting");
    app.restart();
}

/// Spawn a one-shot loopback HTTP server that serves `body` as JSON on
/// the first GET it sees, then shuts down. Returns the `http://127.0.0.1:<port>`
/// URL the caller can hand to `UpdaterBuilder::endpoints`.
///
/// Bind to port 0 so the OS picks a free port — never collide with
/// anything else on the user's box.
///
/// We don't bother with HTTP parsing — the updater client just needs a
/// well-formed JSON response back. Read until the request headers end
/// (CRLFCRLF) then write the response and close.
async fn serve_manifest_once(body: String) -> Result<String, String> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .map_err(|e| format!("bind: {e}"))?;
    let addr = listener
        .local_addr()
        .map_err(|e| format!("local_addr: {e}"))?;
    let url = format!("http://{}/manifest.json", addr);

    tokio::spawn(async move {
        // Accept exactly one connection — the updater's `check()` makes a
        // single GET. After we've served it, drop the listener.
        let (mut stream, _peer) = match listener.accept().await {
            Ok(pair) => pair,
            Err(e) => {
                log(&format!("[updater] loopback accept: {}", e));
                return;
            }
        };

        // Drain request headers. We don't parse them — we just need to wait
        // until the client has finished sending its request line before we
        // reply, otherwise some HTTP clients see ECONNRESET on the send half.
        let mut buf = [0u8; 1024];
        let mut total = Vec::new();
        loop {
            match stream.read(&mut buf).await {
                Ok(0) => break,
                Ok(n) => {
                    total.extend_from_slice(&buf[..n]);
                    if total.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                    if total.len() > 8192 {
                        break;
                    }
                }
                Err(_) => break,
            }
        }

        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            body.len(),
            body
        );
        if let Err(e) = stream.write_all(response.as_bytes()).await {
            log(&format!("[updater] loopback write: {}", e));
        }
        let _ = stream.shutdown().await;
    });

    Ok(url)
}

/// Desktop callback URL — the page creates a sign-in token and deep links back.
/// Not middleware-protected, so it works whether or not the user is signed in yet.
///
/// Previously this URL embedded a Vercel protection-bypass query param so the
/// callback loaded on preview-protected dev.isol8.co. That secret shipped in
/// the binary (and git history) and has been rotated out. If dev.isol8.co
/// preview-protection is re-enabled, pass `LIGHTBULB_CALLBACK_URL` at build time
/// with the appropriate bypass param instead of hard-coding the secret.
const DESKTOP_CALLBACK_URL: &str = match option_env!("LIGHTBULB_CALLBACK_URL") {
    Some(url) => url,
    None => "https://dev.flickmeon.ai/auth/desktop-callback",
};

fn is_oauth_url(url: &Url) -> bool {
    let host = url.host_str().unwrap_or("");
    let path = url.path();
    // Real identity providers: always intercept (Google/Apple OAuth).
    let is_provider = ["accounts.google.com", "appleid.apple.com"]
        .iter()
        .any(|d| host == *d || host.ends_with(&format!(".{}", d)));
    if is_provider {
        return true;
    }
    // Clerk: only intercept the actual OAuth callback path, NOT every hit
    // to clerk.accounts.dev. Endpoints like `/v1/client/handshake` are the
    // normal session-token refresh and MUST stay in the webview — bouncing
    // them to Safari leaves the webview on a white screen because the
    // refresh never completes.
    let is_clerk = ["clerk.accounts.dev", "clerk.com"]
        .iter()
        .any(|d| host == *d || host.ends_with(&format!(".{}", d)));
    if is_clerk && (path == "/v1/oauth_callback" || path.starts_with("/v1/oauth_callback/")) {
        return true;
    }
    false
}

/// Handle a URL arriving via the second-instance argv (macOS protocol-
/// handler launch). Extracts the sign-in ticket if present, focuses the
/// existing main window, and re-emits the `auth:sign-in-ticket` event
/// so the frontend's useDesktopAuth hook can complete the sign-in.
/// Produce a log-safe representation of a URL by dropping the query
/// string and fragment. Applied everywhere we log URLs because
/// /tmp/lightbulb-desktop.log is world-readable — Clerk sign-in tickets,
/// OAuth `code`/`state`, and similar short-lived credentials must not
/// land there.
fn redact_url(url_str: &str) -> String {
    match url::Url::parse(url_str) {
        Ok(u) => format!("{}://{}{}", u.scheme(), u.host_str().unwrap_or(""), u.path()),
        Err(_) => "<unparseable>".to_string(),
    }
}

/// Pick the deep-link URL out of a second-instance argv.
///
/// A plain relaunch (Dock, Finder, Spotlight, `open -a`) forwards only the
/// executable path — and LaunchServices can append `-psn_0_1234` — so *most*
/// second launches carry no URL at all. This deliberately returns an `Option`
/// rather than a parse that can fail: nothing about argv may stop the caller
/// from restoring the window. See `focus_or_recreate_main_window`.
fn deep_link_from_argv(argv: &[String]) -> Option<&str> {
    argv.iter()
        .find(|a| a.starts_with("flickmeon://"))
        .map(|s| s.as_str())
}

/// The two states the main window can be in when we need it back.
#[derive(Debug, PartialEq, Eq)]
pub enum WindowRecovery {
    /// The window object is still alive (hidden, minimized, or just behind
    /// another app): show + unminimize + focus it.
    ShowExisting,
    /// The window was destroyed and the process is running headless: rebuild
    /// it from the bundled config.
    Recreate,
}

pub fn decide_window_recovery(main_window_exists: bool) -> WindowRecovery {
    if main_window_exists {
        WindowRecovery::ShowExisting
    } else {
        WindowRecovery::Recreate
    }
}

/// Bring the main window back, whatever state it is in. Safe to call from any
/// wake-up path (second launch, deep link, tray menu, macOS Dock reopen) and
/// idempotent when the window is already visible and focused.
pub fn focus_or_recreate_main_window(app: &tauri::AppHandle) {
    // Undo an app-level hide (Cmd+H, or Hide from the Dock menu); a hidden
    // NSApplication ignores per-window `show()`.
    #[cfg(target_os = "macos")]
    let _ = app.show();

    let existing = app.get_webview_window("main");
    if decide_window_recovery(existing.is_some()) == WindowRecovery::ShowExisting {
        if let Some(window) = existing {
            let _ = window.unminimize();
            let _ = window.show();
            let _ = window.set_focus();
            log("[window] restored existing main window");
            return;
        }
    }

    // No window object left. Rebuild from the bundled window config so the
    // URL, size and title stay defined once in tauri.conf.json — the prod
    // build patches `.app.windows[0].url` there, so this picks up the right
    // origin in both flavors.
    let config = app
        .config()
        .app
        .windows
        .iter()
        .find(|w| w.label == "main")
        .cloned();
    let Some(config) = config else {
        log("[window] no `main` window config to recreate from");
        return;
    };
    match tauri::WebviewWindowBuilder::from_config(app, &config).and_then(|b| b.build()) {
        Ok(window) => {
            let _ = window.set_focus();
            log("[window] recreated main window from config");
        }
        Err(e) => log(&format!("[window] recreate failed: {e}")),
    }
}

fn handle_deep_link_url(app: &tauri::AppHandle, url_str: &str) {
    log(&format!("[deep-link] received: {}", redact_url(url_str)));
    // Restore the window BEFORE emitting: the ticket event needs a live
    // webview to land in, and a windowless process would drop it silently.
    focus_or_recreate_main_window(app);
    if url_str.starts_with("flickmeon://auth") {
        if let Ok(parsed) = url::Url::parse(url_str) {
            if let Some(ticket) = parsed
                .query_pairs()
                .find(|(k, _)| k == "ticket")
                .map(|(_, v)| v.to_string())
            {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.emit("auth:sign-in-ticket", &ticket);
                    log("[deep-link] emitted auth:sign-in-ticket");
                }
            }
        }
    }
}

#[cfg(test)]
mod window_recovery_tests {
    use super::*;

    #[test]
    fn plain_relaunch_argv_carries_no_deep_link() {
        // The bug: the owner's relaunch forwarded only the executable path,
        // the callback found no flickmeon:// entry, and did nothing at all.
        // `None` here MUST NOT be treated as "nothing to do".
        let argv = vec![
            "/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop".to_string(),
            "-psn_0_886372".to_string(),
        ];
        assert_eq!(deep_link_from_argv(&argv), None);
    }

    #[test]
    fn unparseable_argv_does_not_panic_or_match() {
        let argv = vec!["<unparseable>".to_string(), String::new(), "?!".to_string()];
        assert_eq!(deep_link_from_argv(&argv), None);
        // And the log redaction that produced `<unparseable>` in the report
        // is just a logging concern — it never gates control flow.
        assert_eq!(redact_url("not a url"), "<unparseable>");
    }

    #[test]
    fn deep_link_is_found_among_other_args() {
        let argv = vec![
            "/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop".to_string(),
            "flickmeon://auth?ticket=abc".to_string(),
        ];
        assert_eq!(deep_link_from_argv(&argv), Some("flickmeon://auth?ticket=abc"));
    }

    #[test]
    fn redact_url_drops_the_ticket_query() {
        // /tmp/lightbulb-desktop.log is world-readable; the Clerk one-time
        // sign-in ticket must never reach it.
        let redacted = redact_url("flickmeon://auth?ticket=super-secret#frag");
        assert!(!redacted.contains("super-secret"), "leaked: {redacted}");
    }

    #[test]
    fn missing_window_recreates_rather_than_giving_up() {
        assert_eq!(decide_window_recovery(false), WindowRecovery::Recreate);
        assert_eq!(decide_window_recovery(true), WindowRecovery::ShowExisting);
    }
}

/// Override window.open in the WebView. WKWebView silently blocks
/// popups, so every window.open is a dead click without this: OAuth
/// popups reroute to the desktop sign-in flow, and any other http(s)
/// URL (AWS console quick-create, Stripe dashboard, …) opens in the
/// system browser via the shell plugin (shell:allow-open is granted to
/// the remote origin). Injected from on_page_load — a one-shot eval at
/// setup ran against the provisional document and was wiped when the
/// remote page committed its fresh JS context.
fn window_open_override_script() -> String {
    format!(
        r#"
        (function() {{
            const originalOpen = window.open;
            window.open = function(url, target, features) {{
                const href = url ? String(url) : '';
                if (href.includes('accounts.google.com') || href.includes('clerk')) {{
                    // Redirect to desktop sign-in flow instead
                    window.location.href = '{}';
                    return null;
                }}
                if (/^https?:/i.test(href)) {{
                    window.__TAURI__.core.invoke('plugin:shell|open', {{ path: href }});
                    return null;
                }}
                return originalOpen.call(window, url, target, features);
            }};
        }})();
        "#,
        DESKTOP_CALLBACK_URL
    )
}

pub fn run() {
    tauri::Builder::default()
        // Hide-on-close, not quit-on-close. This process is a background
        // service host — it owns the node host, the gateway WebSocket and the
        // loopback browser proxy the container drives — so tearing it down
        // when the user closes the window would break flows that are supposed
        // to keep running. Preventing the close also means the window object
        // is never destroyed, which is what makes recovery deterministic:
        // there is always something for `focus_or_recreate_main_window` to
        // show. Quit stays one click away in the tray.
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                    log("[window] main close requested; hiding to tray");
                }
            }
        })
        .on_page_load(|webview, payload| {
            if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                log("[page-load] injecting window.open override");
                let _ = webview.eval(&window_open_override_script());
            }
        })
        // MUST be registered before any other plugin so it can hijack
        // launches from a deep-link click: macOS spawns a new process for
        // `flickmeon://...` URLs, and without single_instance the new process
        // starts Tauri fresh — a second window, a fresh webview with no
        // Clerk session, and the ticket never reaches the original app.
        // With single_instance, the second launch's argv gets forwarded
        // to this callback on the ORIGINAL process and we can wake it up.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            // Redact each argv entry before logging. Protocol-handler
            // launches include flickmeon://auth?...&ticket=... in argv, which
            // would leak the Clerk one-time sign-in ticket otherwise.
            let redacted: Vec<String> = argv.iter().map(|a| redact_url(a)).collect();
            log(&format!(
                "[single-instance] second launch argv={:?}",
                redacted
            ));
            // Restore the window FIRST and unconditionally. A plain relaunch
            // (clicking the app icon) forwards no flickmeon:// URL, so the
            // old `for arg in argv { if starts_with(...) }` loop matched
            // nothing and the callback returned having done nothing — every
            // relaunch of a windowless process was silently swallowed.
            focus_or_recreate_main_window(app);
            if let Some(url) = deep_link_from_argv(&argv) {
                handle_deep_link_url(app, url);
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri::plugin::Builder::<tauri::Wry>::new("desktop-approval")
            .invoke_handler(tauri::generate_handler![desktop_approval::desktop_shell_answer]).build())
        .register_uri_scheme_protocol("desktop-approval", |_context, _request| {
            tauri::http::Response::builder()
                .header("Content-Type", "text/html; charset=utf-8")
                .header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src ipc: http://ipc.localhost")
                .body(desktop_approval::HTML.as_bytes().to_vec()).unwrap()
        })
        .plugin(
            tauri::plugin::Builder::<tauri::Wry>::new("oauth-intercept")
                .on_navigation(|_window, url| {
                    // Redact: Clerk OAuth callbacks carry short-lived
                    // `code`/`state` credentials in query params.
                    log(&format!(
                        "[oauth-intercept] navigating to: {}",
                        redact_url(url.as_str())
                    ));
                    if is_oauth_url(url) {
                        log(&format!(
                            "[oauth-intercept] matched OAuth domain; opening callback in system browser: {}",
                            redact_url(DESKTOP_CALLBACK_URL)
                        ));
                        // Use AppleScript so Safari is force-activated to
                        // the foreground. Plain `open -a Safari URL` from
                        // a background app was opening Safari but not
                        // bringing it to front; user would never see the
                        // callback page. `tell application "Safari" ...
                        // activate` explicitly raises Safari.
                        let script = format!(
                            r#"tell application "Safari"
    activate
    open location "{}"
end tell"#,
                            DESKTOP_CALLBACK_URL.replace('"', "\\\"")
                        );
                        let status = std::process::Command::new("osascript")
                            .args(["-e", &script])
                            .status();
                        match status {
                            Ok(s) if s.success() => log("[oauth-intercept] activated Safari via osascript"),
                            Ok(s) => log(&format!("[oauth-intercept] osascript returned {}", s)),
                            Err(e) => log(&format!("[oauth-intercept] osascript failed: {}", e)),
                        }
                        return false;
                    }
                    true
                })
                .build(),
        )
        .manage(AuthState {
            token: Mutex::new(None),
            gateway_url: Mutex::new(None),
            current_jwt: Arc::new(RwLock::new(String::new())),
            bootstrap_in_flight: Mutex::new(false),
            proxy_handle: Mutex::new(None),
            subprocess_handle: Mutex::new(None),
            node_host_mutation: tokio::sync::Mutex::new(()),
            pair_response: Mutex::new(None),
            gateway_token: Arc::new(RwLock::new(None)),
        })
        .manage(NodeState {
            status: Mutex::new("disconnected".into()),
        })
        .manage(local_sessions::enrollment::Controller::default())
        .invoke_handler(tauri::generate_handler![
            local_sessions::enrollment::local_sessions_auth,
            local_sessions::enrollment::local_sessions_status,
            local_sessions::enrollment::local_sessions_set,
            local_sessions::enrollment::local_sessions_revoke,
            send_auth_token,
            is_desktop,
            get_node_status,
            updater_identity,
            apply_update_manifest,
            file_access_list_paths,
            file_access_add_path,
            file_access_remove_path,
            file_access_default_path,
            local_mcp_list,
            local_browser_status,
            local_browser_configure,
            local_browser_pairing,
            local_mcp_set,
            local_mcp_remove,
            claude_export::export_claude_context,
            notify::native_notify,
            notify::notification_permission,
        ])
        .setup(|app| {
            log("[setup] Lightbulb desktop app starting");

            // Seed the file-access allowlist (creates ~/Lightbulb/ and adds
            // it on first launch). Non-fatal on failure — file ops will
            // just be denied with a clear error until the user fixes it
            // via the Settings UI.
            if let Err(e) = file_access::init() {
                log(&format!("[setup] file_access::init failed: {}", e));
            }

            // Force-register flickmeon:// with this running binary's path so
            // macOS LaunchServices dispatches deep links here. Without
            // this, a stale bundle from an earlier build (or a prior
            // worktree) can still be registered as the handler, and
            // Safari clicks on flickmeon://auth?... will launch THAT ghost
            // bundle instead of hitting our on_open_url handler.
            #[cfg(debug_assertions)]
            {
                use tauri_plugin_deep_link::DeepLinkExt;
                match app.deep_link().register_all() {
                    Ok(()) => log("[setup] registered flickmeon:// URL schemes"),
                    Err(e) => log(&format!("[setup] register_all failed: {}", e)),
                }
            }

            tray::create_tray(app.handle())?;

            // Click/foreground delegate only. Permission is asked by the webview
            // once someone signs in (`notify::notification_permission`).
            notify::install(app.handle());

            // Native sharing is an independent consented service. A missing
            // OpenClaw sidecar/gateway cannot prevent a retained pair restoring it.
            let sharing_app = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(e) = sharing_app.state::<local_sessions::enrollment::Controller>().restore(&sharing_app).await {
                    log(&format!("[local-sessions] sharing restore unavailable; check Desktop settings: {e}"));
                }
            });

            // Start the node host from persisted pair state, if any — so a
            // backgrounded launch (login item, relaunch after an update)
            // brings up the full desktop-node surface without waiting for
            // the webview to resolve a Clerk session. First run (nothing
            // persisted) is a silent no-op; the webview pair path handles it.
            let node_start_handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                match start_node_host_from_persisted(&node_start_handle).await {
                    Ok(true) => log("[setup] node host started from persisted pair state"),
                    Ok(false) => {
                        log("[setup] no persisted pair state; waiting for webview sign-in to pair")
                    }
                    Err(e) => {
                        log(&format!("[setup] persisted-state node start failed: {e}"));
                        update_node_status(&node_start_handle, "error");
                    }
                }
            });

            // Handle deep links (flickmeon:// protocol). This fires when the app
            // is already running and a cross-process deep link is delivered
            // by the OS directly (no second process spawn). The parallel
            // single_instance callback above handles the cold-launch case.
            let app_handle = app.handle().clone();
            app.deep_link().on_open_url(move |event| {
                for url_obj in event.urls() {
                    handle_deep_link_url(&app_handle, &url_obj.to_string());
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while running tauri application")
        .run(|_app, _event| {
            // Dock-icon click on an already-running app: macOS delivers
            // applicationShouldHandleReopen instead of spawning a second
            // process, so single_instance never fires for this path.
            #[cfg(target_os = "macos")]
            if matches!(_event, tauri::RunEvent::Reopen { .. }) {
                log("[reopen] dock reopen; restoring main window");
                focus_or_recreate_main_window(_app);
            }
        });
}
