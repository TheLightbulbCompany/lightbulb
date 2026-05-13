//! Node invoke handler — executes local commands on the user's Mac.
//!
//! Ported from OpenClaw's node-host invoke handlers:
//! - src/node-host/invoke.ts (dispatch + result building)
//! - src/node-host/invoke-system-run.ts (process spawning)

use crate::exec_approvals::{self, ApprovalDecision, ExecSecurity};
use crate::node_client::{InvokeError, NodeClient, NodeInvokeRequest, NodeInvokeResult};
use serde::Deserialize;
use std::collections::HashMap;
use std::path::Path;
use tokio::process::Command;

/// Max output size per stream (stdout/stderr) — 200KB, same as OpenClaw
const OUTPUT_CAP: usize = 200 * 1024;

#[derive(Deserialize)]
struct SystemRunParams {
    // OpenClaw's agent passes argv as `command: string[]`; see
    // openclaw/src/node-host/invoke-types.ts:3-17 and the caller at
    // openclaw/src/agents/bash-tools.exec-host-node.ts:106-112.
    #[serde(rename = "command")]
    argv: Vec<String>,
    // Original command as a string — OpenClaw sends this alongside argv
    // so the prepare response can round-trip an authoritative commandText
    // that the approval card displays to the user.
    #[serde(rename = "rawCommand")]
    raw_command: Option<String>,
    cwd: Option<String>,
    env: Option<HashMap<String, String>>,
    #[serde(rename = "timeoutMs")]
    timeout_ms: Option<u64>,
    input: Option<String>,
    // Plan binding fields. The prepare response echoes these back so
    // OpenClaw can tie the subsequent system.run invocation to the
    // originating agent/session for approval bookkeeping.
    #[serde(rename = "agentId")]
    agent_id: Option<String>,
    #[serde(rename = "sessionKey")]
    session_key: Option<String>,
}

#[derive(Deserialize)]
struct WhichParams {
    // OpenClaw's SystemWhichParams uses `bins: string[]`; see
    // openclaw/src/node-host/invoke.ts:53-55.
    #[serde(rename = "bins")]
    names: Vec<String>,
}

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

/// Commands OpenClaw currently tries to invoke on a mac node. Every command
/// that reaches our invoke_rx for dispatch must be advertised in
/// node_client.rs so the gateway routes it here (otherwise OpenClaw refuses
/// to dial the node for that command). NOT_IMPLEMENTED returns are
/// preferable to silent timeouts.
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

/// Dispatch a node.invoke.request to the appropriate handler.
pub async fn handle_invoke(
    client: &NodeClient,
    app: tauri::AppHandle,
    request: NodeInvokeRequest,
) {
    let id = request.id.clone();
    let node_id = request.node_id.clone();
    let command = request.command.clone();

    let result = match command.as_str() {
        "system.run" => handle_system_run(&request).await,
        "system.run.prepare" => handle_system_run_prepare(&request).await,
        "system.which" => handle_system_which(&request).await,
        "system.notify" => handle_system_notify(&request).await,
        "device.info" => handle_device_info(&request).await,
        "device.status" => handle_device_status(&request).await,
        "device.health" => handle_device_health(&request).await,
        "device.permissions" => handle_device_permissions(&request).await,
        "system.execApprovals.get" => Ok(NodeInvokeResult {
            id,
            node_id,
            ok: true,
            payload_json: Some(exec_approvals::get_snapshot()),
            error: None,
        }),
        "system.execApprovals.set" => Ok(NodeInvokeResult {
            id,
            node_id,
            ok: true,
            payload_json: Some(r#"{"ok":true}"#.into()),
            error: None,
        }),
        "browser.proxy" => handle_browser_proxy(&request, app.clone()).await,
        // file-transfer plugin commands (ported from openclaw's
        // extensions/file-transfer/src/node-host/*). Each returns the
        // upstream JSON envelope { ok, ... } directly so agent-side
        // tool wrappers work unmodified. The Rust impls enforce the
        // user-managed allowlist client-side; this is the safety
        // boundary, not just the backend's allowedPaths config.
        "file.fetch" => {
            let params = parse_ft_params(&request.params_json);
            wrap_ft_payload(&request, crate::file_transfer::handle_file_fetch(&params).await)
        }
        "file.write" => {
            let params = parse_ft_params(&request.params_json);
            wrap_ft_payload(&request, crate::file_transfer::handle_file_write(&params).await)
        }
        "dir.list" => {
            let params = parse_ft_params(&request.params_json);
            wrap_ft_payload(&request, crate::file_transfer::handle_dir_list(&params).await)
        }
        "dir.fetch" => {
            let params = parse_ft_params(&request.params_json);
            wrap_ft_payload(&request, crate::file_transfer::handle_dir_fetch(&params).await)
        }
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

async fn handle_system_run(
    request: &NodeInvokeRequest,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    let params: SystemRunParams = parse_params(&request.params_json)?;

    if params.argv.is_empty() {
        return Ok(error_result(request, "INVALID_PARAMS", "argv is required"));
    }

    // Effective cwd — must be computed before argv[0] resolution so the
    // approval key for relative executables (`./tool`) binds to this
    // specific directory. Without this, `./tool` in /home/user/projA and
    // `./tool` in /tmp/evil would share one approval key.
    let cwd = params
        .cwd
        .clone()
        .unwrap_or_else(|| std::env::var("HOME").unwrap_or_else(|_| "/tmp".into()));

    // Resolve argv[0] to an absolute path BEFORE approval checks. The
    // approval store keys on argv[0] — keying on a bare name like "git"
    // would let a binary placed earlier on PATH (e.g. /tmp/evil/git)
    // inherit a prior "Allow Always" approval for /usr/bin/git. Resolving
    // first binds approvals to the specific binary that will actually run,
    // and we spawn against the same resolved path (no re-resolution race).
    let argv = resolve_argv0_absolute(&params.argv, &cwd).await;

    // No node-side approval gate. OpenClaw runs the approval flow on the
    // container (emits exec.approval.requested, waits for the user's
    // decision via the in-chat card, persists allow-always to
    // ~/.openclaw/exec-approvals.json, and only calls system.run on the
    // node AFTER approval). Re-checking here produced a second native
    // macOS dialog on top of the in-chat card — duplicate UX for the
    // same decision. See openclaw/src/agents/bash-tools.exec-host-node.ts
    // for the container-side gate and
    // docs/superpowers/specs/2026-04-18-exec-approval-card-design.md for
    // the in-chat flow.

    let (cmd, args) = argv.split_first().unwrap();

    let mut command = Command::new(cmd);
    command.args(args).current_dir(&cwd);

    // Put the child in its own process group so a timeout-kill can take down
    // its entire subtree (e.g. `bash -c "sleep 999"` spawns `sleep` as a
    // child of bash; killing bash alone leaves sleep running and still
    // holding stdout/stderr open, which would make our drain tasks block
    // on EOF and break timeoutMs). `.process_group(0)` sets pgid = pid.
    // Note: tokio::process::Command exposes this directly under cfg(unix);
    // no trait import needed.
    #[cfg(unix)]
    command.process_group(0);

    // Sanitize environment
    let mut env = sanitize_env();
    if let Some(extra) = params.env {
        env.extend(extra);
    }
    command.envs(env);

    // Handle stdin
    command.stdin(std::process::Stdio::piped());
    command.stdout(std::process::Stdio::piped());
    command.stderr(std::process::Stdio::piped());

    let mut child = command.spawn()?;
    #[cfg(unix)]
    let child_pid: Option<i32> = child.id().map(|p| p as i32);

    // Write stdin if provided
    if let Some(input) = &params.input {
        if let Some(mut stdin) = child.stdin.take() {
            use tokio::io::AsyncWriteExt;
            let _ = stdin.write_all(input.as_bytes()).await;
            drop(stdin);
        }
    } else {
        drop(child.stdin.take());
    }

    // Take stdout/stderr handles so we can drain them concurrently with the
    // wait, and still collect (partial) output if we have to kill the child
    // on timeout.
    //
    // Memory safety: we cap what we STORE at OUTPUT_CAP but keep reading past
    // it so the child's pipe buffers don't fill and block it. A `yes` or
    // `cat /dev/urandom` used to buffer gigabytes in the Vec before truncation
    // and crash the desktop app (panic=abort in release).
    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();
    let stdout_task = tokio::spawn(async move { drain_with_cap(stdout_pipe).await });
    let stderr_task = tokio::spawn(async move { drain_with_cap(stderr_pipe).await });

    // Wait for exit, with optional timeout. On timeout, kill the whole
    // process GROUP — child.kill() only signals the direct pid, which for
    // wrapper commands like `bash -c "sleep 999"` leaves the grandchild
    // alive (and holding stdout/stderr, blocking the drain tasks).
    let (exit_status, timed_out) = if let Some(timeout_ms) = params.timeout_ms {
        match tokio::time::timeout(
            std::time::Duration::from_millis(timeout_ms),
            child.wait(),
        )
        .await
        {
            Ok(status) => (status?, false),
            Err(_) => {
                #[cfg(unix)]
                {
                    if let Some(pid) = child_pid {
                        // killpg(pgid, SIGKILL). Safe because we set
                        // process_group(0) at spawn time — pgid == pid.
                        unsafe {
                            libc::killpg(pid, libc::SIGKILL);
                        }
                    }
                }
                // Still call child.kill() for correctness on non-unix and
                // to reap the direct child's zombie. On unix the pgroup
                // signal already delivered SIGKILL to it.
                let _ = child.kill().await;
                let status = child.wait().await.unwrap_or_default();
                (status, true)
            }
        }
    } else {
        (child.wait().await?, false)
    };

    let stdout_bytes = stdout_task.await.unwrap_or_default();
    let mut stderr_bytes = stderr_task.await.unwrap_or_default();
    if timed_out && stderr_bytes.is_empty() {
        stderr_bytes = b"Process timed out".to_vec();
    }
    let output = std::process::Output {
        status: exit_status,
        stdout: stdout_bytes,
        stderr: stderr_bytes,
    };

    let stdout = truncate_output(&output.stdout);
    let stderr = truncate_output(&output.stderr);
    let exit_code = output.status.code();
    // OpenClaw's RunResult.success is `exitCode === 0 && !timedOut && !error`
    // (openclaw/src/node-host/invoke.ts:268). Spawn errors would have returned
    // earlier with ok:false, so we just check exit + timeout here. The agent
    // reads this field directly (bash-tools.exec-host-node.ts:443,453) and
    // marks any run missing it or with false as failed.
    let success = exit_code == Some(0) && !timed_out;

    let payload = serde_json::json!({
        "exitCode": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "timedOut": timed_out,
        "success": success,
        "error": null,
    });

    Ok(NodeInvokeResult {
        id: request.id.clone(),
        node_id: request.node_id.clone(),
        ok: true,
        payload_json: Some(payload.to_string()),
        error: None,
    })
}

async fn handle_system_run_prepare(
    request: &NodeInvokeRequest,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    let params: SystemRunParams = parse_params(&request.params_json)?;

    if params.argv.is_empty() {
        return Ok(error_result(request, "INVALID_PARAMS", "argv is required"));
    }

    // Resolve argv[0] to an absolute path so system.run and prepare use the
    // SAME path-keyed view when the user later approves / allow-always's
    // the command. Mismatches here would make approval keys unstable.
    let cwd = params
        .cwd
        .clone()
        .unwrap_or_else(|| std::env::var("HOME").unwrap_or_else(|_| "/tmp".into()));
    let argv = resolve_argv0_absolute(&params.argv, &cwd).await;

    // OpenClaw expects { plan: { argv, commandText, cwd, agentId, sessionKey } }
    // — see openclaw/src/infra/system-run-approval-binding.ts:40-67 for the
    // required shape. Returning anything else fails the parse at
    // bash-tools.exec-host-node.ts:117 with "invalid system.run.prepare
    // response". Required fields: non-empty `argv` AND non-empty
    // `commandText`.
    let command_text = params
        .raw_command
        .clone()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| argv.join(" "));
    let payload = serde_json::json!({
        "plan": {
            "argv": argv,
            "cwd": params.cwd,
            "commandText": command_text,
            "agentId": params.agent_id,
            "sessionKey": params.session_key,
        }
    });

    Ok(NodeInvokeResult {
        id: request.id.clone(),
        node_id: request.node_id.clone(),
        ok: true,
        payload_json: Some(payload.to_string()),
        error: None,
    })
}

async fn handle_system_which(
    request: &NodeInvokeRequest,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    let params: WhichParams = parse_params(&request.params_json)?;

    // Match OpenClaw's shape: { bins: { name: path } } containing ONLY the
    // names that resolved. See openclaw/src/node-host/invoke.ts:316-326.
    // system.which has no caller-specified cwd; use HOME as the default
    // target directory, same as handle_system_run's cwd fallback.
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    let mut found: HashMap<String, String> = HashMap::new();
    for name in &params.names {
        let trimmed = name.trim();
        if trimmed.is_empty() {
            continue;
        }
        if let Some(path) = which(trimmed, &home).await {
            found.insert(trimmed.to_string(), path);
        }
    }

    let payload = serde_json::json!({ "bins": found });
    Ok(NodeInvokeResult {
        id: request.id.clone(),
        node_id: request.node_id.clone(),
        ok: true,
        payload_json: Some(payload.to_string()),
        error: None,
    })
}

// --- Exec approval prompt ---

/// Show a native macOS dialog asking the user to approve command execution.
/// Returns the user's decision.
async fn prompt_exec_approval(command_preview: &str) -> ApprovalDecision {
    // Use osascript to show a native dialog with three buttons.
    // This runs on the main thread and blocks until the user responds.
    let script = format!(
        r#"display dialog "Isol8 agent wants to run:\n\n{}" with title "Isol8 - Command Approval" buttons {{"Deny", "Allow Once", "Allow Always"}} default button "Allow Once" with icon caution"#,
        command_preview.replace('"', r#"\""#).replace('\n', r#"\n"#)
    );

    let output = tokio::process::Command::new("osascript")
        .arg("-e")
        .arg(&script)
        .output()
        .await;

    match output {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout);
            if stdout.contains("Allow Always") {
                ApprovalDecision::AllowAlways
            } else if stdout.contains("Allow Once") {
                ApprovalDecision::AllowOnce
            } else {
                ApprovalDecision::Deny
            }
        }
        Err(_) => {
            // Dialog failed to show — deny by default
            ApprovalDecision::Deny
        }
    }
}

// --- Helpers ---

async fn which(name: &str, cwd: &str) -> Option<String> {
    if Path::new(name).is_absolute() {
        if is_executable(name).await {
            return Some(name.into());
        }
        return None;
    }

    // PATH entries may be relative (`.`, `bin`, `./scripts`). Historically
    // we'd resolve these against the backend's process cwd, which is NOT
    // the cwd the command will actually run in. That meant
    // `which("tool")` could fail in the backend, leave argv[0] as the
    // bare name, and the subsequent `Command::new("tool")
    // .current_dir(cwd)` would find `cwd/./tool` at spawn time — bypassing
    // the approval key that was computed against the bare name. Resolve
    // relative PATH entries against the REQUESTED cwd so approval and
    // execution see the same binary.
    let path_var = std::env::var("PATH").unwrap_or_default();
    for dir in path_var.split(':') {
        let full_path = if Path::new(dir).is_absolute() {
            format!("{}/{}", dir, name)
        } else {
            format!("{}/{}/{}", cwd, dir, name)
        };
        if is_executable(&full_path).await {
            // Canonicalize so `<cwd>/./tool` becomes `<cwd>/tool` — stable
            // key across symlinks and redundant path components.
            if let Ok(canon) = tokio::fs::canonicalize(&full_path).await {
                return Some(canon.to_string_lossy().into_owned());
            }
            return Some(full_path);
        }
    }
    None
}

/// Resolve argv[0] to an absolute path, returning a new argv. This is the
/// pre-approval-check normalization that binds approvals to a specific
/// binary on disk — not to a name/path that could alias onto a different
/// binary under a different PATH or cwd.
///
/// Three cases:
/// - **Absolute path** (`/usr/bin/git`): canonicalize (resolve symlinks,
///   normalize `..`) and use that. Stable key across symlink changes.
/// - **Relative path with `/`** (`./tool`, `../bin/x`, `subdir/tool`):
///   resolve against `cwd`. Without this, `./tool` in `/home/user/projA`
///   and `./tool` in `/tmp/evil` collapse to the same approval key —
///   one "Allow Always" covers both directories.
/// - **Bare name** (`git`, `python3`): PATH lookup via `which`.
///
/// If resolution fails in any case, we fall back to the joined path
/// (for relative) or the original string (for bare name) so the
/// approval key at least doesn't alias under cwd changes. The
/// subsequent spawn will reject the non-existent file naturally.
async fn resolve_argv0_absolute(argv: &[String], cwd: &str) -> Vec<String> {
    if argv.is_empty() {
        return argv.to_vec();
    }
    let mut out = argv.to_vec();
    let argv0 = &argv[0];

    if Path::new(argv0).is_absolute() {
        if let Ok(canon) = tokio::fs::canonicalize(argv0).await {
            out[0] = canon.to_string_lossy().into_owned();
        }
    } else if argv0.contains('/') {
        // Relative path — must be bound to cwd, else `./tool` keys alias
        // across different working directories.
        let combined = Path::new(cwd).join(argv0);
        if let Ok(canon) = tokio::fs::canonicalize(&combined).await {
            out[0] = canon.to_string_lossy().into_owned();
        } else {
            // File doesn't exist yet or can't canonicalize — use the
            // joined path as-is so the key at least reflects cwd. Spawn
            // will fail naturally.
            out[0] = combined.to_string_lossy().into_owned();
        }
    } else if let Some(resolved) = which(argv0, cwd).await {
        out[0] = resolved;
    }

    out
}

async fn is_executable(path: &str) -> bool {
    use std::os::unix::fs::PermissionsExt;
    tokio::fs::metadata(path)
        .await
        .map(|m| m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

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
/// outer `error` field so the gateway's upstream see the same status
/// it would for any other handler-returned error.
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

fn truncate_output(bytes: &[u8]) -> String {
    // Cap on bytes (not chars) and convert via from_utf8_lossy so a boundary
    // landing mid-codepoint becomes a replacement char instead of a panic
    // (release builds use panic = "abort", so this would crash the app).
    let slice = if bytes.len() > OUTPUT_CAP {
        &bytes[..OUTPUT_CAP]
    } else {
        bytes
    };
    String::from_utf8_lossy(slice).into_owned()
}

/// Drain a child stdio pipe, keeping at most OUTPUT_CAP bytes in memory.
/// Excess is read and discarded so the pipe doesn't back-pressure the child —
/// otherwise a high-volume writer (yes, cat /dev/urandom) would either block
/// forever or we'd have to kill the child to stop it. We also need the cap
/// enforced *during* reads so we don't OOM before truncation.
async fn drain_with_cap<R>(pipe: Option<R>) -> Vec<u8>
where
    R: tokio::io::AsyncRead + Unpin,
{
    use tokio::io::AsyncReadExt;
    let mut buf = Vec::with_capacity(4096);
    let Some(mut pipe) = pipe else { return buf };
    let mut chunk = [0u8; 8 * 1024];
    loop {
        match pipe.read(&mut chunk).await {
            Ok(0) => break, // EOF
            Ok(n) => {
                if buf.len() < OUTPUT_CAP {
                    let remaining = OUTPUT_CAP - buf.len();
                    let take = n.min(remaining);
                    buf.extend_from_slice(&chunk[..take]);
                }
                // Past the cap: discard rest of this chunk (and future chunks),
                // but keep draining so the child's pipe buffer doesn't fill.
            }
            Err(_) => break,
        }
    }
    buf
}

fn sanitize_env() -> HashMap<String, String> {
    let mut env: HashMap<String, String> = std::env::vars().collect();
    // Remove sensitive Tauri/Node internals
    env.remove("ELECTRON_RUN_AS_NODE");
    env.remove("NODE_OPTIONS");
    env.remove("TAURI_ENV_DEBUG");
    env
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

// ---- browser.proxy ----

#[derive(serde::Deserialize, Debug)]
struct BrowserProxyParams {
    // HTTP method (GET/POST/...) the agent's browser tool wants to
    // invoke against the control service. See
    // apps/macos/Sources/OpenClaw/NodeMode/MacNodeBrowserProxy.swift:81-86
    // for the canonical shape.
    method: Option<String>,
    path: Option<String>,
    #[serde(default)]
    query: Option<serde_json::Value>,
    #[serde(default)]
    body: Option<serde_json::Value>,
    #[serde(default)]
    auth: Option<BrowserProxyAuth>,
}

#[derive(serde::Deserialize, Debug)]
struct BrowserProxyAuth {
    token: Option<String>,
    password: Option<String>,
}

async fn handle_browser_proxy(
    request: &NodeInvokeRequest,
    app: tauri::AppHandle,
) -> Result<NodeInvokeResult, Box<dyn std::error::Error + Send + Sync>> {
    use tauri::Manager;

    let params: BrowserProxyParams = parse_params(&request.params_json)?;
    let method = params.method.unwrap_or_else(|| "GET".into());
    let path = params.path.unwrap_or_else(|| "/".into());

    // Ensure the sidecar handle is initialized, then ensure the
    // subprocess is running. Call start() on every invoke because it's
    // idempotent and respawns a dead child via try_wait() probe — so
    // a sidecar that crashed between invokes gets revived here.
    let handle = app.state::<crate::browser_sidecar::BrowserSidecarHandle>();
    {
        let guard = handle.read().await;
        if guard.is_none() {
            drop(guard);
            let mut w = handle.write().await;
            if w.is_none() {
                let sc = crate::browser_sidecar::BrowserSidecar::for_app(&app)
                    .map_err(|e| format!("sidecar init: {}", e))?;
                *w = Some(sc);
            }
        }
    }
    {
        let guard = handle.read().await;
        if let Some(sc) = guard.as_ref() {
            sc.start().await.map_err(|e| format!("sidecar start: {}", e))?;
        }
    }

    // Port is deterministic (gatewayPort+2 = 18791), but the child
    // takes a moment to bind after spawn — cold start on M-series is
    // ~1-2s. Wait until the port accepts a TCP connection before
    // issuing the first HTTP request, otherwise the caller sees a
    // confusing connection-refused mid-race.
    wait_for_control_port(crate::browser_sidecar::BROWSER_CONTROL_PORT)
        .await
        .map_err(|e| format!("sidecar readiness: {}", e))?;

    let url = build_proxy_url(
        crate::browser_sidecar::BROWSER_CONTROL_PORT,
        &path,
        params.query.as_ref(),
    );
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| format!("http client: {}", e))?;
    let mut req = client.request(
        method.parse().map_err(|e| format!("bad method {}: {}", method, e))?,
        &url,
    );
    if let Some(auth) = params.auth {
        if let Some(token) = auth.token {
            req = req.bearer_auth(token);
        } else if let Some(password) = auth.password {
            req = req.basic_auth("", Some(password));
        }
    }
    if let Some(body) = params.body {
        req = req.json(&body);
    }
    let resp = req.send().await.map_err(|e| format!("http: {}", e))?;
    let status = resp.status().as_u16();
    let bytes = resp.bytes().await.map_err(|e| format!("body read: {}", e))?;

    // Response is JSON most of the time; if it's not, wrap in a
    // text envelope so the client can still read it.
    let body: serde_json::Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(_) => serde_json::Value::String(String::from_utf8_lossy(&bytes).into_owned()),
    };
    let payload = serde_json::json!({
        "status": status,
        "body": body,
    });
    Ok(NodeInvokeResult {
        id: request.id.clone(),
        node_id: request.node_id.clone(),
        ok: true,
        payload_json: Some(payload.to_string()),
        error: None,
    })
}

/// Poll-connect to `127.0.0.1:port` until it accepts or a 10s
/// deadline elapses. Returns Ok as soon as one connection succeeds.
/// Used before the first HTTP request so cold-start races don't
/// surface to the agent as connection-refused.
async fn wait_for_control_port(port: u16) -> Result<(), String> {
    use tokio::net::TcpStream;
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(10);
    loop {
        if TcpStream::connect(("127.0.0.1", port)).await.is_ok() {
            return Ok(());
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(format!(
                "control port {} did not accept within 10s",
                port
            ));
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
}

fn build_proxy_url(port: u16, path: &str, query: Option<&serde_json::Value>) -> String {
    let mut url = format!("http://127.0.0.1:{}{}", port, path);
    if let Some(serde_json::Value::Object(map)) = query {
        let pairs: Vec<String> = map
            .iter()
            .filter_map(|(k, v)| match v {
                serde_json::Value::String(s) => Some(format!(
                    "{}={}",
                    urlencoding::encode(k),
                    urlencoding::encode(s)
                )),
                serde_json::Value::Number(n) => Some(format!(
                    "{}={}",
                    urlencoding::encode(k),
                    n
                )),
                _ => None,
            })
            .collect();
        if !pairs.is_empty() {
            url.push('?');
            url.push_str(&pairs.join("&"));
        }
    }
    url
}

#[cfg(test)]
mod resolve_tests {
    use super::*;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| s.to_string()).collect()
    }

    #[tokio::test]
    async fn relative_path_resolved_against_cwd() {
        // Two different cwds → two different resolved argv[0]s for the
        // same `./tool` input. Without this, approval keys alias across
        // directories.
        let tmp1 = tempfile::tempdir().unwrap();
        let tmp2 = tempfile::tempdir().unwrap();

        // Create a real file in each so canonicalize() returns something
        // (canonicalize requires existence).
        tokio::fs::write(tmp1.path().join("tool"), b"fake").await.unwrap();
        tokio::fs::write(tmp2.path().join("tool"), b"fake").await.unwrap();

        let a = resolve_argv0_absolute(
            &argv(&["./tool", "run"]),
            tmp1.path().to_str().unwrap(),
        )
        .await;
        let b = resolve_argv0_absolute(
            &argv(&["./tool", "run"]),
            tmp2.path().to_str().unwrap(),
        )
        .await;

        assert_ne!(a[0], b[0], "different cwds must produce different argv[0]");
        assert!(a[0].starts_with(&*tmp1.path().canonicalize().unwrap().to_string_lossy()));
        assert!(b[0].starts_with(&*tmp2.path().canonicalize().unwrap().to_string_lossy()));
    }

    #[tokio::test]
    async fn relative_path_falls_back_to_joined_when_missing() {
        // Even if the file doesn't exist yet (so canonicalize fails), we
        // must still produce a cwd-bound key — NOT leave the bare `./foo`
        // that would alias across cwds.
        let tmp = tempfile::tempdir().unwrap();
        let out = resolve_argv0_absolute(
            &argv(&["./nonexistent", "x"]),
            tmp.path().to_str().unwrap(),
        )
        .await;
        assert!(
            out[0].contains("nonexistent"),
            "expected combined path, got {:?}",
            out[0]
        );
        assert_ne!(out[0], "./nonexistent", "must not leave bare relative");
    }

    #[tokio::test]
    async fn bare_name_with_relative_path_entry_resolves_against_cwd() {
        // If PATH contains a relative entry (say "."), `which("tool")` must
        // resolve it relative to the REQUESTED cwd, not the backend's
        // process cwd. Otherwise two different requested cwds with a
        // "tool" binary in each would collapse to different resolutions
        // at approval time vs exec time.
        let tmp = tempfile::tempdir().unwrap();
        let tool_path = tmp.path().join("widget");
        tokio::fs::write(&tool_path, b"fake").await.unwrap();
        // Mark executable.
        let perms = std::os::unix::fs::PermissionsExt::from_mode(0o755);
        tokio::fs::set_permissions(&tool_path, perms).await.unwrap();

        // Force PATH to contain only "." so resolution has to use cwd.
        // (Restore after the test — other tests may rely on real PATH.)
        let original_path = std::env::var("PATH").ok();
        std::env::set_var("PATH", ".");

        let out = resolve_argv0_absolute(
            &argv(&["widget", "run"]),
            tmp.path().to_str().unwrap(),
        )
        .await;

        // Restore PATH before any assertion that could panic.
        if let Some(p) = original_path {
            std::env::set_var("PATH", p);
        } else {
            std::env::remove_var("PATH");
        }

        let expected = tool_path.canonicalize().unwrap();
        assert_eq!(
            out[0],
            expected.to_string_lossy(),
            "relative PATH entry must resolve against cwd, not process cwd"
        );
    }

    #[tokio::test]
    async fn absolute_path_canonicalized() {
        let tmp = tempfile::tempdir().unwrap();
        let real = tmp.path().join("bin");
        tokio::fs::write(&real, b"x").await.unwrap();
        // tmp.path() on some systems is a symlinked path (macOS /var -> /private/var).
        // canonicalize should resolve it.
        let out = resolve_argv0_absolute(
            &argv(&[real.to_str().unwrap()]),
            "/",
        )
        .await;
        let canon = real.canonicalize().unwrap();
        assert_eq!(out[0], canon.to_string_lossy());
    }
}
