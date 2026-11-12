//! The Lightbulb Mac app: one window on voice Home, the 🌐 talk key, the tray, native
//! notifications, sign-in through the system browser (Clerk sign-in ticket by deep link) and the
//! updater. Nothing else runs on this Mac: no sessions, no node, no network engine.

pub mod legacy;
pub mod notify;
mod tray;
mod voice_key;

use tauri::{Emitter, Manager, Url};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_updater::UpdaterExt;

/// File log for a GUI app (its stdout is not visible). Tests only print, so they never write into
/// the installed app's log.
pub fn log(msg: &str) {
    #[cfg(not(test))]
    {
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open("/tmp/lightbulb-desktop.log") {
            let _ = writeln!(f, "{}", msg);
        }
    }
    println!("{}", msg);
}

/// A native macOS alert (the talk key's one-time ask). The page cannot draw, fake or click it.
/// Fail-closed: only the yes label approves. Allow is the
/// first button, because an alert that ends any other way (Escape, abort) reads as the
/// SECOND button: rfd 0.16 maps any other NSAlert return to `Cancel`
/// (`backend/macos/message_dialog.rs`), and tauri-plugin-dialog 2.8.1 maps that `Cancel`
/// to `Custom(<second label>)` (`desktop.rs`). With Cancel first, such an ending read as
/// Allow. Return pressing Allow is standard macOS; a page cannot press keys in this alert.
/// The yes stays the first button, for the reason above.
/// `None`: the dialog never answered, which is not a person's no.
pub(crate) async fn confirm_native_labelled(app: &tauri::AppHandle, title: &str, body: String, yes: &str, no: &str) -> Option<bool> {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind, MessageDialogResult};
    let yes = yes.to_owned();
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog()
        .message(body)
        .title(title)
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(yes.clone(), no.into()))
        .show_with_result(move |res| {
            let _ = tx.send(matches!(res, MessageDialogResult::Custom(ref s) if *s == yes));
        });
    rx.await.ok()
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
    None => "https://thelightbulb.company/auth/desktop-callback",
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

/// The hosted page can call every local command its origin is granted, so a prod build
/// must grant only the prod origin, and a plain build from source must BE prod.
#[cfg(test)]
mod flavor_origin_tests {
    use super::is_oauth_url;
    use serde_json::{json, Value};
    use tauri::Url;

    fn parse(s: &str) -> Value {
        serde_json::from_str(s).unwrap()
    }

    fn loaded_urls(conf: &Value) -> Vec<String> {
        let mut out = vec![
            conf["app"]["windows"][0]["url"].clone(),
            conf["build"]["devUrl"].clone(),
            conf["build"]["frontendDist"].clone(),
        ];
        out.extend(conf["plugins"]["updater"]["endpoints"].as_array().unwrap().iter().cloned());
        out.iter().map(|u| u.as_str().unwrap().to_string()).collect()
    }

    #[test]
    fn start_url_is_voice() {
        for (conf, origin) in [
            (parse(include_str!("../tauri.conf.json")), "https://thelightbulb.company"),
            (parse(include_str!("../tauri.dev.conf.json")), "https://dev.thelightbulb.company"),
        ] {
            for u in &loaded_urls(&conf)[..3] {
                assert_eq!(u, &format!("{origin}/voice"));
            }
        }
    }

    #[test]
    fn voice_and_chat_both_stay_in_the_webview() {
        // No path allow-list: IPC capability is origin/* and the navigation hook only
        // diverts OAuth URLs, so /chat stays reachable alongside /voice.
        for p in ["voice", "chat"] {
            assert!(!is_oauth_url(&Url::parse(&format!("https://thelightbulb.company/{p}")).unwrap()));
        }
        let cap = parse(include_str!("../capabilities/default.json"));
        assert_eq!(cap["remote"]["urls"], json!(["https://thelightbulb.company/*"]));
    }

    #[test]
    fn prod_build_grants_ipc_to_the_prod_origin_only() {
        let conf = parse(include_str!("../tauri.conf.json"));
        assert_eq!(conf["app"]["security"]["capabilities"], json!(["default"]));
        for u in loaded_urls(&conf) {
            assert!(u.starts_with("https://thelightbulb.company/"), "prod config loads {u}");
        }
        let cap = parse(include_str!("../capabilities/default.json"));
        assert_eq!(cap["identifier"], "default");
        assert_eq!(cap["remote"]["urls"], json!(["https://thelightbulb.company/*"]));
    }

    #[test]
    fn dev_flavor_grants_ipc_to_the_dev_origin_only() {
        let prod = parse(include_str!("../tauri.conf.json"));
        let dev = parse(include_str!("../tauri.dev.conf.json"));
        assert_eq!(dev["app"]["security"]["capabilities"], json!(["dev"]));
        for u in loaded_urls(&dev) {
            assert!(u.starts_with("https://dev.thelightbulb.company/"), "dev overlay loads {u}");
        }
        // The overlay replaces the windows array whole: it must stay the prod window with
        // only the URL changed.
        let mut window = dev["app"]["windows"][0].clone();
        window["url"] = prod["app"]["windows"][0]["url"].clone();
        assert_eq!(window, prod["app"]["windows"][0]);
        let cap = parse(include_str!("../capabilities/dev.json"));
        let default = parse(include_str!("../capabilities/default.json"));
        assert_eq!(cap["identifier"], "dev");
        assert_eq!(cap["remote"]["urls"], json!(["https://dev.thelightbulb.company/*"]));
        assert_eq!(cap["permissions"], default["permissions"], "both flavors grant the same commands");
        assert_eq!(cap["windows"], default["windows"]);
    }

    #[test]
    fn unflagged_build_talks_to_prod() {
        if option_env!("LIGHTBULB_CALLBACK_URL").is_none() {
            assert_eq!(super::DESKTOP_CALLBACK_URL, "https://thelightbulb.company/auth/desktop-callback");
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

/// Keeps macOS from napping this process while its window is hidden, for as long as it runs:
/// the talk key works from any app, and a nap coalesces the timers the press and the live call
/// ride on. The `NSAppSleepDisabled` Info.plist key is not honoured (macOS 26). The option does
/// not hold the Mac awake.
fn refuse_app_nap() {
    #[cfg(target_os = "macos")]
    {
        use objc2_foundation::{NSActivityOptions, NSProcessInfo, NSString};
        let activity = NSProcessInfo::processInfo().beginActivityWithOptions_reason(
            NSActivityOptions::UserInitiatedAllowingIdleSystemSleep,
            &NSString::from_str("Answers the talk key and keeps a call live while its window is hidden"),
        );
        // Never ended: the activity lasts as long as the process.
        std::mem::forget(activity);
    }
}

pub fn run() {
    let context = tauri::generate_context!();
    tauri::Builder::default()
        // Hide-on-close, not quit-on-close: the talk key and a live call keep
        // working with the window hidden. Preventing the close also means the
        // window object is never destroyed, so `focus_or_recreate_main_window`
        // always has something to show. Quit stays one click away in the tray.
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Focused(_) = event {
                if window.label() == "main" {
                    voice_key::refresh_pill(window.app_handle()); // "Mic on" shows while the window is behind
                }
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    let _ = window.hide();
                    log("[window] main close requested; hiding to tray");
                }
            }
        })
        .on_page_load(|webview, payload| {
            // Only the site: the mic pill's page must never reach `shell|open`.
            if webview.label() == "main" && matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                log("[page-load] injecting window.open override");
                let _ = webview.eval(window_open_override_script());
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
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
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
        .invoke_handler(tauri::generate_handler![
            updater_identity,
            apply_update_manifest,
            notify::native_notify,
            notify::notification_permission,
            voice_key::voice_mic_live,
        ])
        .setup(|app| {
            log("[setup] Lightbulb desktop app starting");

            refuse_app_nap();
            tauri::async_runtime::spawn_blocking(legacy::clean);

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

            // "Open at login" starts on; after that it is the person's toggle in
            // Settings, so this enables once per install and never again. Debug
            // builds skip it: a login item pointing at target/debug is a trap.
            #[cfg(not(debug_assertions))]
            {
                use tauri_plugin_autostart::ManagerExt;
                if let Ok(dir) = app.path().app_data_dir() {
                    let marker = dir.join("autostart-defaulted");
                    if !marker.exists() {
                        if let Err(e) = app.autolaunch().enable() {
                            log(&format!("[setup] open-at-login default failed: {}", e));
                        }
                        let _ = std::fs::create_dir_all(&dir);
                        let _ = std::fs::write(&marker, b"");
                    }
                }
            }

            tray::create_tray(app.handle())?;
            voice_key::install(app.handle()); // tap or hold 🌐 to talk to Lightbulb

            // Click/foreground delegate only. Permission is asked by the webview
            // once someone signs in (`notify::notification_permission`).
            notify::install(app.handle());

            // Handle deep links (flickmeon:// protocol). This fires when the app
            // is already running and a cross-process deep link is delivered
            // by the OS directly (no second process spawn). The parallel
            // single_instance callback above handles the cold-launch case.
            let app_handle = app.handle().clone();
            app.deep_link().on_open_url(move |event| {
                for url_obj in event.urls() {
                    handle_deep_link_url(&app_handle, url_obj.as_str());
                }
            });

            Ok(())
        })
        .build(context)
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
