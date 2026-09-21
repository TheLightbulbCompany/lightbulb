//! An isolated local window is the only source of an approve-once decision.
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Mutex;
use tauri::{Manager, WebviewUrl, WebviewWindow, WebviewWindowBuilder};
use tokio::sync::{oneshot, watch};

pub const HTML: &str = include_str!("desktop_approval.html");
static PENDING: Mutex<Option<(String, oneshot::Sender<bool>)>> = Mutex::new(None);

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct Approval {
    binding: String,
    workspace: String,
    agent: String,
    device: String,
    run: String,
    argv: Option<Vec<String>>,
    cwd: Option<String>,
    env: Option<std::collections::BTreeMap<String, String>>,
    company: Option<String>,
    native_device: Option<String>,
    operation: Option<Value>,
    expires_at_ms: u64,
}

impl Approval {
    fn valid_operation(&self) -> bool {
        match (
            &self.operation,
            &self.company,
            &self.native_device,
            &self.argv,
            &self.cwd,
            &self.env,
        ) {
            (Some(operation), Some(company), Some(native), None, None, None) => {
                !company.is_empty()
                    && company.len() <= 128
                    && !native.is_empty()
                    && native.len() <= 128
                    && operation.is_object()
                    && operation.to_string().len() <= 32768
                    && operation
                        .get("command")
                        .and_then(Value::as_str)
                        .is_some_and(|s| !s.is_empty() && s.len() <= 128)
                    && operation.get("params").is_some_and(Value::is_object)
            }
            (None, None, None, Some(argv), Some(cwd), Some(env)) => {
                !argv.is_empty()
                    && argv.len() <= 64
                    && !argv.iter().any(|s| s.len() > 4096 || s.contains('\0'))
                    && cwd.starts_with('/')
                    && cwd.len() <= 4096
                    && env.len() <= 16
            }
            _ => false,
        }
    }
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn answer(label: &str, approved: bool) -> Result<(), String> {
    let mut pending = PENDING.lock().map_err(|_| "approval unavailable")?;
    if pending.as_ref().map(|(live, _)| live.as_str()) != Some(label) {
        return Err("no live approval for this window".into());
    }
    let (_, sender) = pending.take().unwrap();
    sender.send(approved).map_err(|_| "approval expired".into())
}

#[tauri::command]
pub fn desktop_shell_answer(window: WebviewWindow, approved: bool) -> Result<(), String> {
    // Window is injected by Tauri; neither label nor binding is an IPC argument.
    answer(window.label(), approved)
}

struct Prompt {
    app: tauri::AppHandle,
    label: String,
}
impl Drop for Prompt {
    fn drop(&mut self) {
        let _ = answer(&self.label, false);
        if let Some(window) = self.app.get_webview_window(&self.label) {
            let _ = window.close();
        }
    }
}

pub async fn prompt(
    app: tauri::AppHandle,
    params: Value,
    mut cancel: watch::Receiver<bool>,
) -> Value {
    let Ok(p) = serde_json::from_value::<Approval>(params.clone()) else {
        return json!({"approved":false});
    };
    let remaining = p.expires_at_ms.saturating_sub(now_ms());
    if *cancel.borrow()
        || remaining == 0
        || remaining > 120_000
        || p.binding.len() != 64
        || !p.binding.bytes().all(|b| b.is_ascii_hexdigit())
        || [&p.workspace, &p.agent, &p.device, &p.run]
            .iter()
            .any(|s| s.is_empty() || s.len() > 128)
        || !p.valid_operation()
    {
        return json!({"approved":false,"binding":p.binding});
    }
    let label = format!("desktop-approval-{}", uuid::Uuid::new_v4());
    let (tx, rx) = oneshot::channel();
    {
        let mut pending = PENDING.lock().unwrap();
        if pending.is_some() {
            return json!({"approved":false,"binding":p.binding});
        }
        *pending = Some((label.clone(), tx));
    }
    let _guard = Prompt {
        app: app.clone(),
        label: label.clone(),
    };
    let window = WebviewWindowBuilder::new(
        &app,
        &label,
        WebviewUrl::CustomProtocol("desktop-approval://localhost/".parse().unwrap()),
    )
    .title("Lightbulb — operation approval")
    .inner_size(640.0, 600.0)
    .resizable(true)
    .initialization_script(format!("window.__desktopApproval = {};", params))
    .on_navigation(|url| url.scheme() == "desktop-approval" && url.host_str() == Some("localhost"))
    .build();
    let Ok(window) = window else {
        return json!({"approved":false,"binding":p.binding});
    };
    window.on_window_event(move |event| {
        if matches!(
            event,
            tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed
        ) {
            let _ = answer(&label, false);
        }
    });
    let approved = tokio::select! {
        biased;
        _ = cancel.changed() => false,
        _ = tokio::time::sleep(std::time::Duration::from_millis(remaining)) => false,
        answer = rx => answer.unwrap_or(false),
    };
    json!({"approved": approved && !*cancel.borrow() && now_ms() < p.expires_at_ms, "binding": p.binding})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn operation_consent_is_typed_and_cannot_mix_shell_approval() {
        let request = json!({"binding":"a".repeat(64),"workspace":"org_1","company":"company_1",
            "agent":"agent_1","run":"run_1","device":"cli","nativeDevice":"native",
            "operation":{"command":"device.info","params":{}},"expiresAtMs":now_ms()+60_000});
        let p: Approval = serde_json::from_value(request.clone()).unwrap();
        assert!(p.valid_operation());
        let mut mixed = request.clone();
        mixed["argv"] = json!(["echo"]);
        assert!(!serde_json::from_value::<Approval>(mixed)
            .unwrap()
            .valid_operation());
        let mut invalid = request;
        invalid["operation"] = json!("not a typed operation");
        assert!(!serde_json::from_value::<Approval>(invalid)
            .unwrap()
            .valid_operation());
    }
    #[test]
    fn main_and_duplicate_or_late_responses_cannot_approve() {
        let (tx, mut rx) = oneshot::channel();
        *PENDING.lock().unwrap() = Some(("desktop-approval-test".into(), tx));
        assert!(answer("main", true).is_err());
        assert!(answer("desktop-approval-test", false).is_ok());
        assert!(!rx.try_recv().unwrap());
        assert!(answer("desktop-approval-test", true).is_err());
        assert!(HTML.contains(".textContent=JSON.stringify"));
        assert!(!HTML.contains("innerHTML"));
    }
}
