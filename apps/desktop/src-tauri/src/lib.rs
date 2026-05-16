pub mod browser_sidecar;
mod exec_approvals;
pub mod file_access;
pub mod file_transfer;
pub mod keychain;
mod node_client;
mod node_invoke;
pub mod pair_client;
pub mod proxy;
pub mod subprocess;
mod tray;

use std::sync::{Arc, Mutex, RwLock};
use tauri::{Emitter, Manager, State, Url};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_updater::UpdaterExt;

/// Shared auth token state
pub struct AuthState {
    pub token: Mutex<Option<String>>,
    /// Shared handle to the running node client's gateway URL. Updated whenever
    /// a fresh JWT arrives so the next reconnect uses current auth.
    pub gateway_url: Mutex<Option<Arc<RwLock<String>>>>,
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

/// Called by the web app to send the Clerk JWT to the Rust backend.
/// Starts the node-host connection directly to the gateway.
#[tauri::command]
fn send_auth_token(
    token: String,
    display_name: String,
    user_id: String,
    state: State<'_, AuthState>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    let mut t = state.token.lock().map_err(|e| e.to_string())?;
    let is_first = t.is_none();
    *t = Some(token.clone());
    log(&format!("[auth] Received JWT ({} chars) for user {} ({})", token.len(), display_name, user_id));

    let ws_url = option_env!("ISOL8_WS_URL").unwrap_or("wss://ws-dev.isol8.co");
    // Use an explicit "/" path before the query. tokio-tungstenite builds
    // the HTTP upgrade request line from the URL's path+query; if the URL
    // is `wss://host?token=…` (no slash), the resulting request line is
    // malformed and AWS ELB rejects with HTTP 400 before API Gateway ever
    // sees it. Browsers normalize this implicitly — native WS clients do
    // not.
    let new_gateway_url = format!("{}/?token={}", ws_url, token);

    // Rotate the running client's URL so the next reconnect uses the fresh
    // token. Clerk refreshes JWTs well before expiry, so we want every update
    // to propagate — not just the first one.
    if !is_first {
        if let Ok(handle_guard) = state.gateway_url.lock() {
            if let Some(handle) = handle_guard.as_ref() {
                if let Ok(mut url) = handle.write() {
                    *url = new_gateway_url;
                    log("[auth] Rotated node client URL with fresh token");
                }
            }
        }
        return Ok(());
    }

    // First token — create the shared URL handle, stash it in state, and spawn
    // the node client.
    let shared_url = Arc::new(RwLock::new(new_gateway_url));
    if let Ok(mut handle_guard) = state.gateway_url.lock() {
        *handle_guard = Some(shared_url.clone());
    }

    let app_handle = app.clone();
    tauri::async_runtime::spawn(async move {
        if let Err(e) = start_node_host(&app_handle, shared_url, &display_name).await {
            log(&format!("[node] Failed to start: {}", e));
            update_node_status(&app_handle, "error");
        }
    });

    Ok(())
}

async fn start_node_host(
    app: &tauri::AppHandle,
    gateway_url: Arc<RwLock<String>>,
    display_name: &str,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    update_node_status(app, "connecting");
    log("[node] Starting node client");

    let mut client = node_client::NodeClient::with_shared_url(gateway_url, display_name);

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

/// Result of an updater check, serialized for the webview.
///
/// `UpToDate` means the running version matches the latest published.
/// `Available` carries the new version + release notes so the UI can
/// surface a friendly prompt before the user opts in to install.
#[derive(serde::Serialize)]
#[serde(rename_all = "snake_case", tag = "status")]
enum UpdateCheckResult {
    UpToDate,
    Available {
        version: String,
        notes: Option<String>,
    },
}

/// Ask the updater endpoint if a newer signed build exists.
///
/// The endpoint URL is in tauri.conf.json (plugins.updater.endpoints) and
/// served by apps/frontend/src/app/api/desktop/update/[target]/[current_version]/route.ts.
/// Returns up_to_date or available — never errors on "no update", only on
/// network / verification failures.
#[tauri::command]
async fn check_for_updates(app: tauri::AppHandle) -> Result<UpdateCheckResult, String> {
    log("[updater] check requested");
    let updater = app.updater().map_err(|e| {
        log(&format!("[updater] failed to acquire updater: {}", e));
        e.to_string()
    })?;
    match updater.check().await {
        Ok(Some(update)) => {
            log(&format!(
                "[updater] update available: {} -> {}",
                update.current_version, update.version
            ));
            Ok(UpdateCheckResult::Available {
                version: update.version.clone(),
                notes: update.body.clone(),
            })
        }
        Ok(None) => {
            log("[updater] up to date");
            Ok(UpdateCheckResult::UpToDate)
        }
        Err(e) => {
            log(&format!("[updater] check failed: {}", e));
            Err(e.to_string())
        }
    }
}

/// Download + install the available update, then restart the app.
///
/// Tauri's updater verifies the bundle's minisign signature against
/// the pubkey in tauri.conf.json before installing — a wrong-key bundle
/// is rejected. After install completes the app is restarted into the
/// new version.
#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<(), String> {
    log("[updater] install requested");
    let updater = app.updater().map_err(|e| e.to_string())?;
    let update = updater
        .check()
        .await
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "no update available".to_string())?;
    log(&format!(
        "[updater] installing {} -> {}",
        update.current_version, update.version
    ));
    update
        .download_and_install(|_chunk, _total| {}, || log("[updater] download finished, installing"))
        .await
        .map_err(|e| {
            log(&format!("[updater] install failed: {}", e));
            e.to_string()
        })?;
    log("[updater] install complete, restarting");
    app.restart();
}

/// OAuth domains that must open in the system browser. Clerk's OAuth flow
/// redirects through Clerk's own frontend API first (e.g.
/// `up-moth-55.clerk.accounts.dev/v1/oauth_callback/...`), THEN to the
/// actual identity provider. We catch the Clerk hop too — otherwise
/// WKWebView tries to navigate cross-origin and the click appears to
/// silently do nothing.
const OAUTH_DOMAINS: &[&str] = &[
    "accounts.google.com",
    "appleid.apple.com",
    "clerk.accounts.dev",
    "clerk.com",
];

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
        })
        .manage(NodeState {
            status: Mutex::new("disconnected".into()),
        })
        .manage::<browser_sidecar::BrowserSidecarHandle>(
            std::sync::Arc::new(tokio::sync::RwLock::new(None)),
        )
        .invoke_handler(tauri::generate_handler![
            send_auth_token,
            is_desktop,
            get_node_status,
            check_for_updates,
            install_update,
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
