pub mod bootstrap;
pub mod file_access;
pub mod keychain;
mod node_client;
mod node_invoke;
pub mod pair_client;
pub mod proxy;
pub mod screen;
pub mod subprocess;
mod tray;

use std::sync::{Arc, Mutex, RwLock};
use tauri::{Emitter, Manager, State, Url};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_updater::UpdaterExt;

use crate::bootstrap::{bootstrap_desktop_node_host, jwt_provider_from_arc, DesktopNodeHostHandles};

/// Keychain service name for the desktop process. Matches the bundle ID so
/// macOS surfaces Keychain entries as belonging to this app.
const KEYCHAIN_SERVICE: &str = "co.isol8.desktop";

/// HTTPS API base for the backend. Overridden at build time when a non-dev
/// build is produced.
const ISOL8_API_BASE: &str = match option_env!("ISOL8_API_BASE") {
    Some(v) => v,
    None => "https://api-dev.isol8.co",
};

/// Shared auth token state
pub struct AuthState {
    pub token: Mutex<Option<String>>,
    /// Shared handle to the running node client's gateway URL. Updated whenever
    /// a fresh JWT arrives so the next reconnect uses current auth.
    ///
    /// Doubles as the "bootstrap completed" sentinel — only populated AFTER
    /// `run_bootstrap` returns Ok. If bootstrap fails on the first call (a
    /// transient `/desktop/pair` 5xx, a Keychain permission prompt, or a
    /// missing sidecar binary), this stays `None` so the next
    /// `send_auth_token` retries bootstrap rather than dropping straight to
    /// the URL-rotation branch. Codex round-3 P2 on PR #667.
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
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open("/tmp/isol8-desktop.log") {
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
        // Regression test for Codex round-3 P2 on PR #667: if
        // bootstrap fails (transient /desktop/pair error, Keychain
        // permission denial, missing sidecar binary), gateway_url MUST
        // stay None so the next send_auth_token retries. Previously
        // is_first was keyed off `token`, which was set BEFORE the
        // spawned bootstrap task, so every retry silently dropped into
        // the rotation branch without ever bringing the desktop node
        // back up. Now `bootstrap_completed` is keyed off the URL
        // handle that is only published after bootstrap succeeds.
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
///      this used to silently fall through to the rotation branch
///      because the `token` was already set, even though the
///      proxy/subprocess never came up. Codex round-3 P2 on PR #667.
#[tauri::command]
fn send_auth_token(
    token: String,
    display_name: String,
    user_id: String,
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

    let ws_url = option_env!("ISOL8_WS_URL").unwrap_or("wss://ws-dev.isol8.co");
    // Use an explicit "/" path before the query. tokio-tungstenite builds
    // the HTTP upgrade request line from the URL's path+query; if the URL
    // is `wss://host?token=…` (no slash), the resulting request line is
    // malformed and AWS ELB rejects with HTTP 400 before API Gateway ever
    // sees it. Browsers normalize this implicitly — native WS clients do
    // not.
    // clientId is surfaced to the $connect Lambda (Task 9) and on to the
    // backend's ws_connect (Tasks 10 + 14), which uses it to skip the
    // synthetic Ed25519 challenge for the Tauri-mac process. The real
    // challenge is the upstream container's challenge, signed by Tauri-mac
    // locally with the Mac-slot Keychain key in node_client::perform_handshake.
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
    tauri::async_runtime::spawn(async move {
        let result = run_bootstrap(&app_handle, jwt_arc, &display_name_for_chain).await;

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
) -> Result<(), String> {
    let _ = display_name; // reserved for telemetry hooks

    let jwt_snapshot = jwt_arc
        .read()
        .map(|g| g.clone())
        .map_err(|e| format!("jwt read: {e}"))?;
    if jwt_snapshot.is_empty() {
        return Err("bootstrap called with empty JWT".into());
    }

    // The Tauri path resolver lives behind `Manager::path()` in Tauri 2.
    // `app_data_dir` is the per-app data dir (macOS:
    // ~/Library/Application Support/co.isol8.desktop); `resource_dir` is
    // the bundle's Resources/ directory in release builds.
    let app_data = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("app_data_dir: {e}"))?;
    let state_dir = app_data.join("openclaw-state");

    // The launcher shim is registered as `bin/isol8-browser-service` in
    // `tauri.conf.json` (externalBin). Tauri's bundler finds
    // `bin/isol8-browser-service-<target-triple>` at build time, strips
    // the triple, and places the file in the `.app` at
    // `Contents/MacOS/isol8-browser-service` — alongside the main Tauri
    // executable. Dev builds replicate the layout next to
    // `target/debug/<exe>`. So we resolve via current_exe().parent().
    //
    // Task 20 made `isol8-browser-service` read `ISOL8_PROXY_PORT` and
    // dial loopback plaintext WS, which is what `spawn_subprocess` is
    // designed to talk to.
    let exe = std::env::current_exe().map_err(|e| format!("current_exe: {e}"))?;
    let parent = exe
        .parent()
        .ok_or_else(|| "current_exe has no parent".to_string())?;
    let binary = parent.join("isol8-browser-service");
    if !binary.exists() {
        return Err(format!(
            "isol8-browser-service shim not found at {}",
            binary.display()
        ));
    }

    log(&format!(
        "[bootstrap] api_base={} state_dir={:?} binary={:?}",
        ISOL8_API_BASE, state_dir, binary
    ));

    let provider = jwt_provider_from_arc(jwt_arc);
    let handles: DesktopNodeHostHandles = bootstrap_desktop_node_host(
        ISOL8_API_BASE,
        &jwt_snapshot,
        KEYCHAIN_SERVICE,
        "node-host".to_string(),
        state_dir,
        binary,
        provider,
    )
    .await?;

    log(&format!(
        "[bootstrap] paired device_id_mac={} device_id_cli={} proxy_port={}",
        handles.pair_response.device_id_mac,
        handles.pair_response.device_id_cli,
        handles.proxy_handle.local_port()
    ));

    // Stash on state so the handles outlive the spawned task. We grab the
    // state lazily here because there's no `State<'_, AuthState>` in this
    // async context.
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

    Ok(())
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

    let mut client = node_client::NodeClient::with_shared_url(
        gateway_url,
        display_name,
        identity,
        gateway_token,
    );

    // Flip the tray/renderer status ONLY when the node client emits a real
    // transition. Previously we said "connected" right after start() returned,
    // but start() just spawns the connection loop — the WS handshake hadn't
    // happened yet. A bad token or network hiccup left the UI claiming
    // connected while the node was actually still dialing.
    let app_for_cb = app.clone();
    client.on_status_change(move |status| {
        update_node_status(&app_for_cb, status);
    });

    let mut invoke_rx = client.start().await?;
    log("[node] Node client started, listening for invoke requests");

    let app_for_invoke = app.clone();
    tokio::spawn(async move {
        while let Some(request) = invoke_rx.recv().await {
            log(&format!("[node] Invoke: {} ({})", request.command, request.id));
            node_invoke::handle_invoke(&client, app_for_invoke.clone(), request).await;
        }
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
        .map_err(|e| format!("loopback manifest server: {e}"))?;
    log(&format!("[updater] loopback manifest endpoint: {}", endpoint));

    let endpoint_url = Url::parse(&endpoint)
        .map_err(|e| format!("loopback url parse: {e}"))?;

    let updater = app
        .updater_builder()
        .endpoints(vec![endpoint_url])
        .map_err(|e| format!("set endpoints: {e}"))?
        // The plugin's default comparator does `release.version > current_version`.
        // Our backend route already gated on `isNewer(latestVersion, currentVersion)`
        // (returning 204 otherwise), so by the time we get here the webview has
        // already confirmed there IS a newer version. Force-accept the comparison
        // so any tag-format mismatch can't drop us into a silent no-op.
        .version_comparator(|_current, _release| true)
        .build()
        .map_err(|e| format!("build updater: {e}"))?;

    let update = updater
        .check()
        .await
        .map_err(|e| format!("updater check: {e}"))?
        .ok_or_else(|| "updater returned no update for webview-supplied manifest".to_string())?;

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
/// preview-protection is re-enabled, pass `ISOL8_CALLBACK_URL` at build time
/// with the appropriate bypass param instead of hard-coding the secret.
const DESKTOP_CALLBACK_URL: &str = match option_env!("ISOL8_CALLBACK_URL") {
    Some(url) => url,
    None => "https://dev.isol8.co/auth/desktop-callback",
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
/// /tmp/isol8-desktop.log is world-readable — Clerk sign-in tickets,
/// OAuth `code`/`state`, and similar short-lived credentials must not
/// land there.
fn redact_url(url_str: &str) -> String {
    match url::Url::parse(url_str) {
        Ok(u) => format!("{}://{}{}", u.scheme(), u.host_str().unwrap_or(""), u.path()),
        Err(_) => "<unparseable>".to_string(),
    }
}

fn handle_deep_link_url(app: &tauri::AppHandle, url_str: &str) {
    log(&format!("[deep-link] received: {}", redact_url(url_str)));
    if url_str.starts_with("isol8://auth") {
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
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

pub fn run() {
    tauri::Builder::default()
        // MUST be registered before any other plugin so it can hijack
        // launches from a deep-link click: macOS spawns a new process for
        // `isol8://...` URLs, and without single_instance the new process
        // starts Tauri fresh — a second window, a fresh webview with no
        // Clerk session, and the ticket never reaches the original app.
        // With single_instance, the second launch's argv gets forwarded
        // to this callback on the ORIGINAL process and we can wake it up.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            // Redact each argv entry before logging. Protocol-handler
            // launches include isol8://auth?...&ticket=... in argv, which
            // would leak the Clerk one-time sign-in ticket otherwise.
            let redacted: Vec<String> = argv.iter().map(|a| redact_url(a)).collect();
            log(&format!(
                "[single-instance] second launch argv={:?}",
                redacted
            ));
            // The deep-link URL appears as one of the argv entries.
            for arg in argv {
                if arg.starts_with("isol8://") {
                    handle_deep_link_url(app, &arg);
                }
            }
        }))
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_dialog::init())
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
            pair_response: Mutex::new(None),
            gateway_token: Arc::new(RwLock::new(None)),
        })
        .manage(NodeState {
            status: Mutex::new("disconnected".into()),
        })
        .invoke_handler(tauri::generate_handler![
            send_auth_token,
            is_desktop,
            get_node_status,
            updater_identity,
            apply_update_manifest,
            file_access_list_paths,
            file_access_add_path,
            file_access_remove_path,
            file_access_default_path,
        ])
        .setup(|app| {
            log("[setup] Isol8 desktop app starting");

            // Seed the file-access allowlist (creates ~/Isol8/ and adds
            // it on first launch). Non-fatal on failure — file ops will
            // just be denied with a clear error until the user fixes it
            // via the Settings UI.
            if let Err(e) = file_access::init() {
                log(&format!("[setup] file_access::init failed: {}", e));
            }

            // Force-register isol8:// with this running binary's path so
            // macOS LaunchServices dispatches deep links here. Without
            // this, a stale bundle from an earlier build (or a prior
            // worktree) can still be registered as the handler, and
            // Safari clicks on isol8://auth?... will launch THAT ghost
            // bundle instead of hitting our on_open_url handler.
            #[cfg(debug_assertions)]
            {
                use tauri_plugin_deep_link::DeepLinkExt;
                match app.deep_link().register_all() {
                    Ok(()) => log("[setup] registered isol8:// URL schemes"),
                    Err(e) => log(&format!("[setup] register_all failed: {}", e)),
                }
            }

            tray::create_tray(app.handle())?;

            // Override window.open in the WebView so OAuth popups open
            // in the system browser. WKWebView silently blocks popups,
            // so Clerk's Google OAuth popup never opens without this.
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.eval(&format!(
                    r#"
                    (function() {{
                        const originalOpen = window.open;
                        window.open = function(url, target, features) {{
                            if (url && (url.includes('accounts.google.com') || url.includes('clerk'))) {{
                                // Redirect to desktop sign-in flow instead
                                window.location.href = '{}';
                                return null;
                            }}
                            return originalOpen.call(window, url, target, features);
                        }};
                    }})();
                    "#,
                    DESKTOP_CALLBACK_URL
                ));
            }

            // Handle deep links (isol8:// protocol). This fires when the app
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
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
