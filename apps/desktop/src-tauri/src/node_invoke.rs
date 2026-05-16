//! Node invoke handler — Mac-native commands only.
//!
//! Post-Phase-4 split: the cross-platform commands (`system.run*`,
//! `system.which`, `system.execApprovals.*`, `browser.proxy`,
//! `file.fetch`/`file.write`, `dir.list`/`dir.fetch`) now live in the
//! openclaw subprocess (see `apps/desktop/src-tauri/bin/openclaw-host`).
//! What remains here is the macOS-platform surface that the subprocess
//! can't (or shouldn't) re-implement: native notifications, device
//! probes, and screen snapshot. `screen.snapshot`'s implementation
//! still lives in `file_transfer.rs` for now — Task 24 will either
//! relocate it here or fold it into a dedicated module before deleting
//! `file_transfer.rs`.
//!
//! Original upstream references (kept for context until the subprocess
//! has full parity): openclaw/src/node-host/invoke.ts (dispatch) +
//! openclaw/src/node-host/invoke-types.ts (param shapes).

use crate::node_client::{InvokeError, NodeClient, NodeInvokeRequest, NodeInvokeResult};
use serde::Deserialize;
use tokio::process::Command;

#[derive(Deserialize)]
struct NotifyParams {
    title: Option<String>,
    body: Option<String>,
    sound: Option<String>,
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
/// The `_app` handle is currently unused but retained for symmetry with
/// the future Mac-native surface (camera/photos/etc. will need it for
/// permission prompts) and to avoid churning `lib.rs` in this trim.
pub async fn handle_invoke(
    client: &NodeClient,
    _app: tauri::AppHandle,
    request: NodeInvokeRequest,
) {
    let id = request.id.clone();
    let node_id = request.node_id.clone();
    let command = request.command.clone();

    let result = match command.as_str() {
        "system.notify" => handle_system_notify(&request).await,
        "device.info" => handle_device_info(&request).await,
        "device.status" => handle_device_status(&request).await,
        "device.health" => handle_device_health(&request).await,
        "device.permissions" => handle_device_permissions(&request).await,
        "screen.snapshot" => {
            let params = parse_ft_params(&request.params_json);
            wrap_ft_payload(
                &request,
                crate::file_transfer::handle_screen_snapshot(&params).await,
            )
        }
        cmd if NOT_IMPLEMENTED_COMMANDS.contains(&cmd) => Ok(NodeInvokeResult {
            id,
            node_id,
            ok: false,
            payload_json: None,
            error: Some(InvokeError {
                code: "NOT_IMPLEMENTED".into(),
                message: format!(
                    "{} is not yet supported on the Isol8 desktop node. See docs for node capabilities.",
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
    };

    match result {
        Ok(invoke_result) => {
            if let Err(e) = client.send_invoke_result(invoke_result).await {
                eprintln!("[node-invoke] Failed to send result: {}", e);
            }
        }
        Err(e) => {
            let _ = client
                .send_invoke_result(NodeInvokeResult {
                    id: request.id.clone(),
                    node_id: request.node_id.clone(),
                    ok: false,
                    payload_json: None,
                    error: Some(InvokeError {
                        code: "INTERNAL".into(),
                        message: e.to_string(),
                    }),
                })
                .await;
        }
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
/// to Null when missing or malformed. The file-transfer handlers each
/// re-validate the shape themselves, so a Null here just propagates as
/// "missing required field" through their normal error paths.
fn parse_ft_params(params_json: &Option<String>) -> serde_json::Value {
    match params_json {
        Some(s) => serde_json::from_str(s).unwrap_or(serde_json::Value::Null),
        None => serde_json::Value::Null,
    }
}

/// Wrap a file_transfer handler's `{ok, code?, message?, ...}` payload
/// in the dispatch loop's NodeInvokeResult shape. The payload becomes
/// `payload_json`; on `ok:false` we ALSO mirror code/message into the
/// outer `error` field so the gateway's upstream sees the same status
/// it would for any other handler-returned error. Kept here for the
/// `screen.snapshot` arm until Task 24 relocates the screen handler.
fn wrap_ft_payload(
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
                    .unwrap_or("file-transfer handler error")
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

// ---- system.notify ----

/// Escape a string for safe embedding inside an AppleScript double-quoted
/// literal. AppleScript treats `\` and `"` as special; everything else
/// (including newlines) is taken literally. Mirrors what OpenClaw's Mac app
/// does in Objective-C — we use osascript instead of UserNotifications
/// because the UN framework requires bundle entitlements we'd rather not
/// ship a first release with.
fn escape_applescript_string(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

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

    let mut script = format!(
        r#"display notification "{}" with title "{}""#,
        escape_applescript_string(&body),
        escape_applescript_string(&title),
    );
    if let Some(sound) = params.sound.as_deref().filter(|s| !s.is_empty()) {
        script.push_str(&format!(
            r#" sound name "{}""#,
            escape_applescript_string(sound),
        ));
    }

    let output = Command::new("osascript")
        .args(["-e", &script])
        .output()
        .await?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).to_string();
        return Ok(error_result(request, "NOTIFY_FAILED", stderr.trim()));
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
        Command::new(cmd).args(args).output(),
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
            "app": "Isol8 Desktop",
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
    // We don't (yet) request camera/photos/screen/location permissions from
    // macOS, so reporting the TCC state here would be misleading — the only
    // honest answer is "we haven't asked". When those capabilities land we
    // can probe the actual authorization status via AVFoundation/
    // CoreLocation/Photos/ScreenCaptureKit and swap this out.
    let payload = serde_json::json!({
        "camera": "not-requested",
        "microphone": "not-requested",
        "photos": "not-requested",
        "location": "not-requested",
        "screenRecording": "not-requested",
        "notifications": "granted",  // osascript display notification works without explicit grant
        "accessibility": "not-requested",
    });
    Ok(ok_payload(request, payload))
}
