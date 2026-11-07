//! Node invoke handler — Mac-native commands only.
//!
//! Post-Phase-4 split: the cross-platform commands (`system.run*`,
//! `system.which`, `system.execApprovals.*`, `browser.proxy`,
//! `file.fetch`/`file.write`, `dir.list`/`dir.fetch`) now live in the
//! openclaw subprocess (see `apps/desktop/src-tauri/bin/openclaw-host`).
//! What remains here is the macOS-platform surface that the subprocess
//! can't (or shouldn't) re-implement: native notifications, device
//! probes, and screen snapshot. `screen.snapshot`'s implementation
//! lives in `crate::screen`.
//!
//! Original upstream references (kept for context until the subprocess
//! has full parity): openclaw/src/node-host/invoke.ts (dispatch) +
//! openclaw/src/node-host/invoke-types.ts (param shapes).

use crate::node_client::NodeEvent;
use crate::node_client::{InvokeError, NodeClient, NodeInvokeRequest, NodeInvokeResult};
use futures_util::{stream::FuturesUnordered, StreamExt};
use serde::Deserialize;
use std::collections::{HashMap, VecDeque};
use tokio::process::Command;
use tokio::sync::{mpsc, watch};

#[derive(Default)]
struct Invocations {
    active: HashMap<String, (String, watch::Sender<bool>)>,
    seen: VecDeque<(String, String, std::time::Instant)>,
    generation: u64,
}

impl Invocations {
    fn admit(&mut self, request: &NodeInvokeRequest) -> Option<watch::Receiver<bool>> {
        while self
            .seen
            .front()
            .is_some_and(|(_, _, time)| time.elapsed().as_secs() > 310)
        {
            self.seen.pop_front();
        }
        if self.active.len() >= 8
            || self.seen.len() >= 128
            || self.active.contains_key(&request.id)
            || self
                .seen
                .iter()
                .any(|(id, node, _)| id == &request.id && node == &request.node_id)
        {
            return None;
        }
        self.seen.push_back((
            request.id.clone(),
            request.node_id.clone(),
            std::time::Instant::now(),
        ));
        let (tx, rx) = watch::channel(false);
        self.active
            .insert(request.id.clone(), (request.node_id.clone(), tx));
        Some(rx)
    }

    fn cancel(&mut self, id: String, node: String) {
        if let Some((live_node, tx)) = self.active.get(&id) {
            if live_node == &node {
                tx.send_replace(true);
            }
        }
        if self.seen.len() < 128 {
            self.seen.push_back((id, node, std::time::Instant::now()));
        }
    }

    fn disconnect(&mut self) {
        self.generation += 1;
        for (_, tx) in self.active.values() {
            tx.send_replace(true);
        }
    }
}

pub async fn run(
    client: NodeClient,
    app: tauri::AppHandle,
    mut events: mpsc::Receiver<NodeEvent>,
    chrome: crate::agent_chrome::Channels,
    mut cdp_events: mpsc::Receiver<crate::node_client::CdpInbound>,
) {
    // CDP channel events on their own task: `on_event` never awaits, so a Chrome launch
    // never stalls invokes, cancels or other channels' frames.
    let cdp_chrome = chrome.clone();
    let cdp_task = tokio::spawn(async move {
        while let Some((event, payload)) = cdp_events.recv().await {
            // A shared session's offered channel first (session_browser), then the runs' own.
            if !crate::session_browser::on_event(&cdp_chrome, &event, &payload) {
                cdp_chrome.on_event(&event, &payload);
            }
        }
    });
    let mut jobs = FuturesUnordered::new();
    let mut registry = Invocations::default();
    loop {
        tokio::select! {
            biased;
            event = events.recv() => {
                match event {
                    Some(NodeEvent::Disconnected) | None => {
                        registry.disconnect();
                        chrome.close_all(None); // the socket is gone
                        if event.is_none() {
                            cdp_task.abort();
                            while jobs.next().await.is_some() {}
                            break;
                        }
                    }
                    Some(NodeEvent::Cancel { invoke_id, node_id }) => {
                        registry.cancel(invoke_id, node_id);
                    }
                    Some(NodeEvent::Invoke(request)) => {
                        let Some(rx) = registry.admit(&request) else {
                            let _ = client.send_invoke_result(error_result(&request, "REJECTED", "cancelled, duplicate, or at capacity")).await;
                            continue;
                        };
                        let tx = registry.active[&request.id].1.clone();
                        let app = app.clone();
                        let admitted_generation = registry.generation;
                        jobs.push(async move {
                            let deadline = std::time::Duration::from_millis(request.timeout_ms.unwrap_or(30_000).clamp(1, 180_000));
                            let operation = handle_invoke(app, request.clone(), rx.clone());
                            tokio::pin!(operation);
                            let result = tokio::select! {
                                result = &mut operation => result,
                                _ = tokio::time::sleep(deadline) => {
                                    let _ = tx.send(true);
                                    // Handlers cooperate: capture kills AND waits before returning.
                                    operation.await
                                }
                            };
                            (request.id, admitted_generation, result)
                        });
                    }
                }
            }
            Some((id, admitted_generation, result)) = jobs.next(), if !jobs.is_empty() => {
                registry.active.remove(&id);
                if admitted_generation == registry.generation { let _ = client.send_invoke_result(result).await; }
            }
        }
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    fn request(id: &str) -> NodeInvokeRequest {
        NodeInvokeRequest {
            id: id.into(),
            node_id: "mac".into(),
            command: "device.health".into(),
            params_json: None,
            timeout_ms: Some(100),
        }
    }

    #[test]
    fn cancel_before_spawn_exact_node_capacity_disconnect_and_late_result() {
        let mut registry = Invocations::default();
        registry.cancel("cancelled".into(), "mac".into());
        assert!(registry.admit(&request("cancelled")).is_none());
        let receiver = registry.admit(&request("live")).unwrap();
        registry.cancel("live".into(), "other-mac".into());
        assert!(!*receiver.borrow());
        registry.cancel("live".into(), "mac".into());
        assert!(*receiver.borrow());
        assert!(registry.admit(&request("live")).is_none());
        for n in 0..7 {
            registry.admit(&request(&n.to_string())).unwrap();
        }
        assert!(registry.admit(&request("ninth")).is_none());
        let admitted_generation = registry.generation;
        registry.disconnect();
        assert_ne!(admitted_generation, registry.generation);
        for (_, tx) in registry.active.values() {
            assert!(*tx.borrow());
        }
    }

    #[test]
    fn notification_text_is_literal_and_bypass_fields_are_rejected() {
        let body = "\"; do shell script \"touch /tmp/unwanted\"\n\\";
        let parsed: NotifyParams =
            serde_json::from_value(serde_json::json!({"body": body})).unwrap();
        assert_eq!(parsed.body.as_deref(), Some(body));
        assert!(serde_json::from_value::<NotifyParams>(
            serde_json::json!({"body": body,"approved":true})
        )
        .is_err());
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NotifyParams {
    title: Option<String>,
    body: Option<String>,
    #[serde(rename = "sound")]
    _sound: Option<String>,
    // priority and delivery are accepted but mapped best-effort only — macOS
    // display notification doesn't expose priority/delivery controls.
    #[serde(default, rename = "priority")]
    _priority: Option<String>,
    #[serde(default, rename = "delivery")]
    _delivery: Option<String>,
}

/// Commands OpenClaw currently tries to invoke on a mac node that we
/// don't (yet) implement. Returning NOT_IMPLEMENTED here is preferable
/// to a silent timeout — OpenClaw will route around the capability
/// instead of waiting. The cross-platform set (system.run, file.*,
/// browser.proxy, etc.) is handled by the openclaw subprocess and
/// never reaches this dispatcher.
const NOT_IMPLEMENTED_COMMANDS: &[&str] = &[
    "camera.list",
    "camera.snap",
    "camera.clip",
    "photos.latest",
    "screen.record",
    "location.get",
    "notifications.list",
    "notifications.actions",
];

/// Dispatch a node.invoke.request to the appropriate Mac-native handler.
///
/// Approvals are answered from the consent stored at pairing; capture cooperates
/// with cancellation to reap its child before returning. Other handlers are cancelled by dropping work.
pub async fn handle_invoke(
    app: tauri::AppHandle,
    request: NodeInvokeRequest,
    cancel: watch::Receiver<bool>,
) -> NodeInvokeResult {
    let id = request.id.clone();
    let node_id = request.node_id.clone();
    let command = request.command.clone();

    if *cancel.borrow() {
        return error_result(&request, "CANCELLED", "cancelled before dispatch");
    }
    if command.starts_with("screen.") && !crate::desktop_permissions::load(&app).screen {
        return error_result(&request, "SCREEN_OFF", "Screen is switched off for this Mac in Settings");
    }
    if command == "screen.snapshot" {
        return wrap_screen_payload(
            &request,
            crate::screen::handle_screen_snapshot(
                &parse_screen_params(&request.params_json),
                cancel,
            )
            .await,
        )
        .unwrap();
    }
    let work = async {
        match command.as_str() {
        "file.access.policy.v1" if !crate::desktop_permissions::load(&app).files => Ok(error_result(
            &request, "FILES_OFF", "Files are switched off for this Mac in Settings")),
        "file.access.policy.v1" => match crate::file_access::execution_policy() {
            Ok(policy) => Ok(ok_payload(&request, policy)),
            Err(_) => Ok(error_result(&request, "FILE_POLICY_UNAVAILABLE", "No current approved folders")),
        },
        // Answered from the consent given at pairing (desktop_permissions); no window.
        "system.run.approve.v1" | "desktop.operation.approve.v1" => Ok(ok_payload(&request,
            crate::desktop_permissions::decide(&app, parse_screen_params(&request.params_json)))),
        "browser.cdp.v1" if !crate::desktop_permissions::load(&app).browser => Ok(error_result(
            &request, "BROWSER_OFF", "Browser is switched off for this Mac in Settings")),
        "browser.cdp.v1" => Ok(browser_cdp(&app, &request).await),
        // No switch check: a wipe is exactly what Browser-off and unpair want.
        "browser.wipe.v1" => Ok(browser_wipe(&app, &request).await),
        "session.options.v1" => Ok(ok_payload(&request, crate::session_start::options(&app))),
        "session.start.v1" => Ok(match crate::session_start::start(
            app.clone(), parse_screen_params(&request.params_json), cancel.clone()).await {
            Ok(payload) => ok_payload(&request, payload),
            Err((code, message)) => error_result(&request, code, &message),
        }),
        "system.notify" => handle_system_notify(&request).await,
        "device.info" => handle_device_info(&request).await,
        "device.status" => handle_device_status(&request).await,
        "device.health" => handle_device_health(&request).await,
        "device.permissions" => handle_device_permissions(&request).await,
        cmd if NOT_IMPLEMENTED_COMMANDS.contains(&cmd) => Ok(NodeInvokeResult {
            id,
            node_id,
            ok: false,
            payload_json: None,
            error: Some(InvokeError {
                code: "NOT_IMPLEMENTED".into(),
                message: format!(
                    "{} is not yet supported on the Lightbulb desktop node. See docs for node capabilities.",
                    cmd,
                ),
            }),
        }),
        _ => Ok(NodeInvokeResult {
            id,
            node_id,
            ok: false,
            payload_json: None,
            error: Some(InvokeError {
                code: "UNAVAILABLE".into(),
                message: format!("Unknown command: {}", command),
            }),
        }),
    }
    };
    let mut stop = cancel.clone();
    tokio::select! {
        biased;
        _ = stop.changed() => error_result(&request, "CANCELLED", "operation cancelled"),
        result = work => result.unwrap_or_else(|e| error_result(&request, "INTERNAL", &e.to_string())),
    }
}

// --- Helpers ---

fn parse_params<T: serde::de::DeserializeOwned>(
    params_json: &Option<String>,
) -> Result<T, Box<dyn std::error::Error + Send + Sync>> {
    match params_json {
        Some(json) => Ok(serde_json::from_str(json)?),
        None => Err("Missing params".into()),
    }
}

/// Decode the inbound params_json into a serde_json::Value, defaulting
/// to Null when missing or malformed. The `screen.snapshot` handler
/// re-validates the shape itself, so a Null here just propagates as
/// "missing required field" through its normal error paths.
fn parse_screen_params(params_json: &Option<String>) -> serde_json::Value {
    match params_json {
        Some(s) => serde_json::from_str(s).unwrap_or(serde_json::Value::Null),
        None => serde_json::Value::Null,
    }
}

/// Wrap a `screen.snapshot` handler's `{ok, code?, message?, ...}` payload
/// in the dispatch loop's NodeInvokeResult shape. The payload becomes
/// `payload_json`; on `ok:false` we ALSO mirror code/message into the
/// outer `error` field so the gateway's upstream sees the same status
/// it would for any other handler-returned error.
fn wrap_screen_payload(
    request: &NodeInvokeRequest,
    payload: serde_json::Value,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    let ok = payload.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    Ok(NodeInvokeResult {
        id: request.id.clone(),
        node_id: request.node_id.clone(),
        ok,
        payload_json: Some(payload.to_string()),
        error: if ok {
            None
        } else {
            Some(InvokeError {
                code: payload
                    .get("code")
                    .and_then(|v| v.as_str())
                    .unwrap_or("INVOKE_FAILED")
                    .to_string(),
                message: payload
                    .get("message")
                    .and_then(|v| v.as_str())
                    .unwrap_or("screen.snapshot handler error")
                    .to_string(),
            })
        },
    })
}

fn error_result(request: &NodeInvokeRequest, code: &str, message: &str) -> NodeInvokeResult {
    NodeInvokeResult {
        id: request.id.clone(),
        node_id: request.node_id.clone(),
        ok: false,
        payload_json: None,
        error: Some(InvokeError {
            code: code.into(),
            message: message.into(),
        }),
    }
}

/// Convenience: wrap a serde_json::Value into a successful NodeInvokeResult.
fn ok_payload(request: &NodeInvokeRequest, payload: serde_json::Value) -> NodeInvokeResult {
    NodeInvokeResult {
        id: request.id.clone(),
        node_id: request.node_id.clone(),
        ok: true,
        payload_json: Some(payload.to_string()),
        error: None,
    }
}

// ---- browser.cdp.v1 / browser.wipe.v1 ----

async fn browser_cdp(app: &tauri::AppHandle, request: &NodeInvokeRequest) -> NodeInvokeResult {
    use tauri::Manager;
    let Ok(app_data) = app.path().app_data_dir() else {
        return error_result(request, "CHROME_START_FAILED", "no app data dir");
    };
    if parse_screen_params(&request.params_json).get("action").and_then(|a| a.as_str()) != Some("ensure") {
        return error_result(request, "INVALID_PARAMS", "action must be ensure");
    }
    match crate::agent_chrome::ensure_running(&app_data).await {
        Ok(endpoint) => {
            // Up with no channel yet: quits after the idle window unless one opens.
            crate::with_agent_chrome_channels(app, |c| c.arm_idle());
            ok_payload(request, serde_json::json!({"profileId": endpoint.profile_id}))
        }
        Err((code, message)) => error_result(request, code, &message),
    }
}

async fn browser_wipe(app: &tauri::AppHandle, request: &NodeInvokeRequest) -> NodeInvokeResult {
    use tauri::Manager;
    crate::with_agent_chrome_channels(app, |c| c.close_all(Some("PROFILE_CHANGED")));
    let Ok(app_data) = app.path().app_data_dir() else {
        return error_result(request, "WIPE_FAILED", "no app data dir");
    };
    match tokio::task::spawn_blocking(move || crate::agent_chrome::wipe(&app_data)).await {
        Ok(Ok(wiped)) => ok_payload(request, serde_json::json!({"wiped": wiped})),
        Ok(Err(message)) => error_result(request, "WIPE_FAILED", &message),
        Err(e) => error_result(request, "WIPE_FAILED", &e.to_string()),
    }
}

// ---- system.notify ----

async fn handle_system_notify(
    request: &NodeInvokeRequest,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    let params: NotifyParams = parse_params(&request.params_json)?;
    let title = params.title.unwrap_or_default();
    let body = params.body.unwrap_or_default();
    if title.trim().is_empty() && body.trim().is_empty() {
        return Ok(error_result(
            request,
            "INVALID_PARAMS",
            "title or body is required",
        ));
    }

    if title.len() > 256 || body.len() > 4096 {
        return Ok(error_result(
            request,
            "INVALID_PARAMS",
            "notification too large",
        ));
    }
    if !crate::notify::show(title, Some(body), Some(request.id.clone()), None, None).await {
        return Ok(error_result(
            request,
            "PERMISSION_DENIED",
            "notifications unavailable or denied",
        ));
    }

    Ok(NodeInvokeResult {
        id: request.id.clone(),
        node_id: request.node_id.clone(),
        ok: true,
        payload_json: Some(r#"{"ok":true}"#.into()),
        error: None,
    })
}

// ---- device.* ----

/// Run a read-only command with a 3s cap and return trimmed stdout, or
/// empty string on any error. Used for `sw_vers`, `uname`, `hostname`, etc.
/// Never fails — these are informational-only fields on device.info.
async fn quick_output(cmd: &str, args: &[&str]) -> String {
    match tokio::time::timeout(
        std::time::Duration::from_secs(3),
        Command::new(cmd).kill_on_drop(true).args(args).output(),
    )
    .await
    {
        Ok(Ok(o)) if o.status.success() => String::from_utf8_lossy(&o.stdout).trim().to_string(),
        _ => String::new(),
    }
}

async fn handle_device_info(
    request: &NodeInvokeRequest,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    let product_name = quick_output("sw_vers", &["-productName"]).await;
    let product_version = quick_output("sw_vers", &["-productVersion"]).await;
    let build_version = quick_output("sw_vers", &["-buildVersion"]).await;
    let hostname = quick_output("hostname", &[]).await;
    let kernel = quick_output("uname", &["-sr"]).await;

    let payload = serde_json::json!({
        "platform": std::env::consts::OS,
        "arch": std::env::consts::ARCH,
        "hostname": if hostname.is_empty() { serde_json::Value::Null } else { serde_json::Value::String(hostname) },
        "os": {
            "name": if product_name.is_empty() { "macOS".to_string() } else { product_name },
            "version": product_version,
            "build": build_version,
        },
        "kernel": kernel,
        "desktop": {
            "app": "Lightbulb Desktop",
            "version": env!("CARGO_PKG_VERSION"),
        },
    });
    Ok(ok_payload(request, payload))
}

async fn handle_device_status(
    request: &NodeInvokeRequest,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    // kern.boottime: "{ sec = 1776000000, usec = 0 } Sun ... "
    let boottime_raw = quick_output("sysctl", &["-n", "kern.boottime"]).await;
    let boot_secs = boottime_raw
        .split("sec = ")
        .nth(1)
        .and_then(|s| s.split(',').next())
        .and_then(|s| s.trim().parse::<u64>().ok());
    let uptime_secs = boot_secs.map(|b| {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs().saturating_sub(b))
            .unwrap_or(0)
    });

    let mem_total_raw = quick_output("sysctl", &["-n", "hw.memsize"]).await;
    let mem_total_bytes: Option<u64> = mem_total_raw.parse().ok();

    let payload = serde_json::json!({
        "ts": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0),
        "uptimeSec": uptime_secs,
        "memory": {
            "totalBytes": mem_total_bytes,
        },
    });
    Ok(ok_payload(request, payload))
}

async fn handle_device_health(
    request: &NodeInvokeRequest,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    let payload = serde_json::json!({
        "ok": true,
        "ts": std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0),
    });
    Ok(ok_payload(request, payload))
}

async fn handle_device_permissions(
    request: &NodeInvokeRequest,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    // Screen and notification permissions are read from macOS without prompts.
    // The remaining capabilities have no implemented handler.
    let payload = serde_json::json!({
        "camera": "not-requested",
        "microphone": "not-requested",
        "photos": "not-requested",
        "location": "not-requested",
        "screenRecording": crate::screen::permission(),
        "notifications": crate::notify::notification_permission(false).await,
        "accessibility": "not-requested",
    });
    Ok(ok_payload(request, payload))
}
