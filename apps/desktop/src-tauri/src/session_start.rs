//! Start a Claude Code / Codex session on this Mac from Lightbulb (owner only).
//!
//! The backend only proves the request is from the human who paired this Mac. Here
//! every field is re-validated against this Mac's own truth: the tool must be an
//! installed fixed binary, the folder one of the approved folders (Settings → File
//! access), model/mode from a closed list, and the prompt is passed after `--` so it
//! can never become a flag. Then Terminal opens the session so the owner sees it;
//! sharing must already be on so it mirrors.
//!
//! No native approve-once window: `session.start.v1` is owner-only end to end. The
//! backend resolves it only to the caller's OWN Mac (the ready row and the reciprocal
//! pairing must both name the Clerk user asking, and the command is granted only to
//! that pairing), so the requester is always the person at this Mac, starting a
//! session he just typed. What reaches the CLI is a fixed installed binary in an
//! approved folder, with no permission-bypass mode, opened visibly in Terminal where
//! the CLI's own approvals still apply. A second "are you sure?" for your own click
//! added nothing. A teammate cannot start sessions here at all.
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use tauri::Manager;
use tokio::sync::watch;

const CLAUDE_MODELS: &[(&str, &str)] =
    &[("opus", "Opus"), ("sonnet", "Sonnet"), ("fable", "Fable")];
const CLAUDE_MODES: &[(&str, &str)] = &[
    ("auto", "Auto"),
    ("manual", "Ask permissions"),
    ("acceptEdits", "Accept edits"),
    ("plan", "Plan"),
];
const CODEX_MODES: &[(&str, &str)] = &[
    ("auto-review", "Approve for me"),
    ("on-request", "Ask on Mac"),
];
const CODEX_BASIC_MODES: &[(&str, &str)] = &[("on-request", "Ask on Mac")];
const CLAUDE_EFFORTS: &[(&str, &str)] = &[
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "Extra high"),
    ("max", "Maximum"),
];
// Common to every currently listed model; model-specific higher levels can be
// offered when the options API exposes each model's capabilities.
const CODEX_EFFORTS: &[(&str, &str)] = &[
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "Extra high"),
];
const MAX_PROMPT: usize = 8192;

type Failure = (&'static str, String);

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct Start {
    tool: String,
    folder: String,
    model: Option<String>,
    mode: Option<String>,
    effort: Option<String>,
    prompt: String,
    start_id: String,
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_default()
}

fn pairs(list: &[(&str, &str)]) -> Vec<Value> {
    list.iter()
        .map(|(id, label)| json!({"id": id, "label": label}))
        .collect()
}

fn codex_help(program: &Path) -> String {
    std::process::Command::new(program)
        .arg("--help")
        .env_remove("CODEX_HOME")
        .stdin(std::process::Stdio::null())
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| {
            format!(
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            )
        })
        .unwrap_or_default()
}

/// Models the Codex CLI itself lists (its own models cache), never a hard-coded guess.
fn codex_models(home: &Path) -> Vec<Value> {
    let Ok(text) = std::fs::read_to_string(home.join(".codex/models_cache.json")) else {
        return vec![];
    };
    let Ok(cache) = serde_json::from_str::<Value>(&text) else {
        return vec![];
    };
    cache["models"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|m| m["visibility"] == "list")
        .filter_map(|m| {
            let id = m["slug"].as_str().filter(|s| valid_model(s))?;
            Some(json!({"id": id, "label": m["display_name"].as_str().unwrap_or(id)}))
        })
        .take(20)
        .collect()
}

fn valid_model(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && !s.starts_with('-')
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
}

fn tools(home: &Path) -> Vec<Value> {
    let mut out = vec![];
    if crate::local_sessions::provider_program(home, "claude").is_some() {
        out.push(
            json!({"id":"claude","label":"Claude Code","modeLabel":"Permission mode",
            "models":pairs(CLAUDE_MODELS),"modes":pairs(CLAUDE_MODES),
            "efforts":pairs(CLAUDE_EFFORTS),"defaultMode":"manual","defaultEffort":null}),
        );
    }
    if let Some(program) = crate::local_sessions::provider_program(home, "codex") {
        let help = codex_help(&program);
        // A dedicated CLI writer keeps a phone launch separate from the shared
        // app-server daemon's other threads, so its start hook can be linked exactly.
        if help.contains("--no-daemon") {
            let auto_review = help.contains("--approve-for-me");
            out.push(
                json!({"id":"codex","label":"Codex","modeLabel":"Approval policy",
                "models":codex_models(home),"modes":pairs(if auto_review { CODEX_MODES } else { CODEX_BASIC_MODES }),
                "efforts":pairs(CODEX_EFFORTS),"defaultMode":"on-request","defaultEffort":null}),
            );
        }
    }
    out
}

fn approved_folders() -> Vec<String> {
    crate::file_access::list_paths()
        .into_iter()
        .filter(|p| Path::new(p).is_dir())
        .collect()
}

/// The name the owner gave this Mac (System Settings → General → About), for the web.
fn mac_name() -> Option<String> {
    let out = std::process::Command::new("/usr/sbin/scutil")
        .args(["--get", "ComputerName"])
        .output()
        .ok()?;
    let name = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (out.status.success() && !name.is_empty()).then(|| name.chars().take(120).collect())
}

pub fn options(app: &tauri::AppHandle) -> Value {
    json!({
        "name": mac_name(),
        "sharing": crate::local_sessions::enrollment::sharing_on(app),
        "folders": approved_folders(),
        "tools": tools(&home()),
    })
}

fn listed(options: &[Value], value: &Option<String>) -> bool {
    value
        .as_deref()
        .map_or(true, |v| options.iter().any(|o| o["id"] == v))
}

/// The exact argv Terminal will run: fixed binary, closed flags, prompt after `--`.
fn argv(program: &Path, start: &Start) -> Vec<String> {
    let mut argv = vec![program.to_string_lossy().into_owned()];
    if let Some(model) = &start.model {
        argv.extend([
            (if start.tool == "codex" {
                "-m"
            } else {
                "--model"
            })
            .into(),
            model.clone(),
        ]);
    }
    if start.tool == "codex" {
        argv.push("--no-daemon".into());
        // Explicitly override a Mac's global config: a missing selection must
        // never inherit approval_policy=never + danger-full-access (YOLO mode).
        argv.extend(["--sandbox".into(), "workspace-write".into()]);
        if start.mode.as_deref() != Some("auto-review") {
            argv.extend(["--ask-for-approval".into(), "on-request".into()]);
        } else {
            argv.push("--approve-for-me".into());
        }
        if let Some(effort) = &start.effort {
            argv.extend(["-c".into(), format!("model_reasoning_effort=\"{effort}\"")]);
        }
    } else {
        argv.extend([
            "--permission-mode".into(),
            start.mode.clone().unwrap_or_else(|| "manual".into()),
        ]);
        if let Some(effort) = &start.effort {
            argv.extend(["--effort".into(), effort.clone()]);
        }
    }
    if !start.prompt.trim().is_empty() {
        argv.extend(["--".into(), start.prompt.clone()]);
    }
    argv
}

fn validate(params: Value, home: &Path) -> Result<(Start, PathBuf, Vec<String>), Failure> {
    let invalid = |m: &str| ("INVALID_REQUEST", m.to_string());
    let start: Start = serde_json::from_value(params).map_err(|_| invalid("Malformed request."))?;
    if start.prompt.len() > MAX_PROMPT || start.prompt.contains('\0') {
        return Err(invalid("The prompt is too long."));
    }
    // Claude's CLI dispatches a lone subcommand name (mcp, update, ...) even after `--`,
    // so a one-word lowercase prompt would run something other than what was approved.
    let word = start.prompt.trim();
    if start.tool == "claude"
        && !word.is_empty()
        && word.bytes().next().is_some_and(|b| b.is_ascii_lowercase())
        && word.bytes().all(|b| b.is_ascii_lowercase() || b == b'-')
    {
        return Err(invalid(
            "Write a few more words: a one-word first message could run a Claude command.",
        ));
    }
    if start.start_id.is_empty()
        || start.start_id.len() > 64
        || !start
            .start_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err(invalid("Malformed request."));
    }
    let tool = tools(home)
        .into_iter()
        .find(|t| t["id"] == start.tool.as_str())
        .ok_or((
            "TOOL_UNAVAILABLE",
            format!("{} is not installed on this Mac.", start.tool),
        ))?;
    let empty = vec![];
    if !listed(tool["models"].as_array().unwrap_or(&empty), &start.model)
        || !listed(tool["modes"].as_array().unwrap_or(&empty), &start.mode)
        || !listed(tool["efforts"].as_array().unwrap_or(&empty), &start.effort)
    {
        return Err(invalid(
            "That model, effort or mode is not available on this Mac.",
        ));
    }
    // Exactly one of the approved folders, compared canonically; never a sub-path or
    // anything the request spells differently.
    let folder = crate::file_access::canonicalize(&start.folder)
        .ok()
        .filter(|f| f.is_dir())
        .filter(|f| {
            approved_folders()
                .iter()
                .filter_map(|a| crate::file_access::canonicalize(a).ok())
                .any(|a| &a == f)
        })
        .ok_or((
            "FOLDER_NOT_APPROVED",
            "That folder is not one of this Mac's approved folders.".into(),
        ))?;
    let program = crate::local_sessions::provider_program(home, &start.tool).ok_or((
        "TOOL_UNAVAILABLE",
        "The CLI is not installed on this Mac.".into(),
    ))?;
    let argv = argv(&program, &start);
    Ok((start, folder, argv))
}

pub(crate) fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// A private one-shot `.command` script: Terminal runs it in a login shell the owner
/// can see, it deletes itself, then execs the CLI. No shell ever parses request text
/// unquoted.
pub(crate) fn script(folder: &Path, path: &Path, argv: &[String]) -> String {
    format!(
        "#!/bin/zsh -l\nrm -f -- {}\ncd -- {} || exit 1\nexec {}\n",
        quote(&path.to_string_lossy()),
        quote(&folder.to_string_lossy()),
        argv.iter().map(|a| quote(a)).collect::<Vec<_>>().join(" ")
    )
}

fn script_with_link(folder: &Path, path: &Path, argv: &[String], start_id: &str, scope: &str) -> String {
    format!(
        "#!/bin/zsh -l\nrm -f -- {}\ncd -- {} || exit 1\nexec /usr/bin/env LIGHTBULB_SESSION_START_ID={} LIGHTBULB_SESSION_SCOPE={} LIGHTBULB_SESSION_LAUNCH_PID=$$ {}\n",
        quote(&path.to_string_lossy()),
        quote(&folder.to_string_lossy()),
        quote(start_id),
        quote(scope),
        argv.iter().map(|a| quote(a)).collect::<Vec<_>>().join(" ")
    )
}

pub(crate) fn launch(
    dir: &Path,
    folder: &Path,
    argv: &[String],
    start_id: &str,
) -> Result<(), Failure> {
    launch_inner(dir, folder, argv, start_id, None)
}

fn launch_with_link(
    dir: &Path,
    folder: &Path,
    argv: &[String],
    start_id: &str,
    scope: &str,
) -> Result<(), Failure> {
    launch_inner(dir, folder, argv, start_id, Some(scope))
}

fn launch_inner(
    dir: &Path,
    folder: &Path,
    argv: &[String],
    start_id: &str,
    scope: Option<&str>,
) -> Result<(), Failure> {
    use std::io::Write;
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let failed = |e: std::io::Error| ("LAUNCH_FAILED", format!("Terminal didn't open: {e}"));
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
        .map_err(failed)?;
    let path = dir.join(format!("{start_id}.command"));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o700)
        .open(&path)
        .map_err(failed)?;
    let command = scope.map_or_else(
        || script(folder, &path, argv),
        |scope| script_with_link(folder, &path, argv, start_id, scope),
    );
    file.write_all(command.as_bytes())
        .map_err(failed)?;
    drop(file);
    let out = std::process::Command::new("/usr/bin/open")
        .args(["-a", "Terminal"])
        .arg(&path)
        .output()
        .map_err(failed)?;
    if !out.status.success() {
        let _ = std::fs::remove_file(&path);
        let why = String::from_utf8_lossy(&out.stderr)
            .trim()
            .chars()
            .take(200)
            .collect::<String>();
        return Err((
            "LAUNCH_FAILED",
            format!(
                "Terminal didn't open: {}",
                if why.is_empty() {
                    out.status.to_string()
                } else {
                    why
                }
            ),
        ));
    }
    Ok(())
}

pub async fn start(
    app: tauri::AppHandle,
    params: Value,
    cancel: watch::Receiver<bool>,
) -> Result<Value, Failure> {
    let result = start_inner(&app, params, &cancel);
    match &result {
        Ok(v) => crate::log(&format!("[session-start] launched {}", v["tool"])),
        Err((code, message)) => crate::log(&format!("[session-start] failed {code}: {message}")),
    }
    result
}

fn start_inner(
    app: &tauri::AppHandle,
    params: Value,
    cancel: &watch::Receiver<bool>,
) -> Result<Value, Failure> {
    let (start, folder, argv) = validate(params, &home())?;
    crate::log(&format!(
        "[session-start] {} {} in {}",
        start.start_id,
        start.tool,
        folder.display()
    ));
    if !crate::local_sessions::enrollment::sharing_on(app) {
        return Err((
            "SHARING_OFF",
            "Turn on session sharing in the Lightbulb app on this Mac first.".into(),
        ));
    }
    if *cancel.borrow() {
        return Err(("CANCELLED", "The start was cancelled.".into()));
    }
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|_| {
            (
                "LAUNCH_FAILED",
                "Terminal didn't open: desktop storage unavailable.".to_string(),
            )
        })?
        .join("session-launch");
    let scope = crate::local_sessions::enrollment::launch_scope(app).map_err(|_| (
        "SHARING_OFF", "Turn on session sharing in Lightbulb on this Mac first.".into()
    ))?;
    launch_with_link(&dir, &folder, &argv, &start.start_id, &scope)?;
    Ok(json!({"started": true, "tool": start.tool}))
}

/// The argv a holder runs for a start. With a start link, `/bin/sh` exports the link with its own
/// pid and then execs the provider in place, so the launch pid is the provider's pid, as the
/// `.command` script does for Terminal.
fn held_argv(argv: &[String], link: Option<(&str, &str)>) -> Vec<String> {
    let Some((start_id, scope)) = link else { return argv.to_vec() };
    let mut out: Vec<String> = vec![
        "/bin/sh".into(),
        "-c".into(),
        r#"export LIGHTBULB_SESSION_START_ID="$1" LIGHTBULB_SESSION_SCOPE="$2" LIGHTBULB_SESSION_LAUNCH_PID=$$; shift 2; exec "$@""#.into(),
        "lightbulb-start".into(),
        start_id.into(),
        scope.into(),
    ];
    out.extend(argv.iter().cloned());
    out
}

/// This Mac's tools, approved folders and name, for the desktop app's own New session view.
#[tauri::command]
pub fn terminal_options(app: tauri::AppHandle) -> Value {
    options(&app)
}

/// From the desktop app's own New session view: the same checks as a remote start (installed
/// tool, approved folder, closed model and mode lists, prompt after `--`), then a held session
/// in a terminal window instead of Terminal.app.
#[tauri::command]
pub async fn terminal_start(app: tauri::AppHandle, params: Value) -> Result<Value, String> {
    let (start, folder, argv) = validate(params, &home()).map_err(|(_, message)| message)?;
    // With sharing on, link the start so the view that asked becomes the session once it reports.
    let scope = crate::local_sessions::enrollment::launch_scope(&app).ok();
    let argv = held_argv(&argv, scope.as_deref().map(|s| (start.start_id.as_str(), s)));
    let info = tauri::async_runtime::spawn_blocking(move || crate::holder::manager::start(&folder, &argv))
        .await
        .map_err(|e| e.to_string())??;
    crate::holder::window::open_window(&app, &info)?;
    crate::log(&format!("[session-start] held {} {}", start.start_id, start.tool));
    Ok(json!({"holderId": info.id, "tool": start.tool, "linked": scope.is_some()}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn start(tool: &str, model: Option<&str>, mode: Option<&str>, prompt: &str) -> Start {
        Start {
            tool: tool.into(),
            folder: "/tmp".into(),
            model: model.map(Into::into),
            mode: mode.map(Into::into),
            effort: None,
            prompt: prompt.into(),
            start_id: "s-1".into(),
        }
    }

    #[test]
    fn session_start_prompt_can_never_become_a_flag() {
        let claude = argv(
            Path::new("/bin/claude"),
            &start(
                "claude",
                Some("opus"),
                Some("plan"),
                "--dangerously-skip-permissions",
            ),
        );
        assert_eq!(
            claude,
            [
                "/bin/claude",
                "--model",
                "opus",
                "--permission-mode",
                "plan",
                "--",
                "--dangerously-skip-permissions"
            ]
        );
        let codex = argv(
            Path::new("/bin/codex"),
            &start("codex", None, Some("on-request"), ""),
        );
        assert_eq!(
            codex,
            [
                "/bin/codex",
                "--no-daemon",
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "on-request"
            ]
        );
    }

    #[test]
    fn session_start_defaults_override_unsafe_global_permissions() {
        assert_eq!(
            argv(Path::new("/bin/codex"), &start("codex", None, None, "")),
            [
                "/bin/codex",
                "--no-daemon",
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "on-request"
            ]
        );
        assert_eq!(
            argv(Path::new("/bin/claude"), &start("claude", None, None, "")),
            ["/bin/claude", "--permission-mode", "manual"]
        );
        let mut codex = start("codex", Some("gpt-6-luna"), Some("on-request"), "");
        codex.effort = Some("high".into());
        assert_eq!(
            argv(Path::new("/bin/codex"), &codex),
            [
                "/bin/codex",
                "-m",
                "gpt-6-luna",
                "--no-daemon",
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "on-request",
                "-c",
                "model_reasoning_effort=\"high\""
            ]
        );
    }

    #[test]
    fn session_start_script_quotes_every_argument() {
        let s = script_with_link(
            Path::new("/Users/a/it's"),
            Path::new("/tmp/x.command"),
            &[
                "/bin/claude".into(),
                "--".into(),
                "$(rm -rf ~); echo 'hi'\nnext".into(),
            ],
            "11111111-1111-4111-8111-111111111111",
            &"a".repeat(64),
        );
        assert!(s.contains("rm -f -- '/tmp/x.command'\ncd -- '/Users/a/it'\\''s' || exit 1"));
        assert!(s.contains("LIGHTBULB_SESSION_START_ID='11111111-1111-4111-8111-111111111111'"));
        assert!(s.contains("LIGHTBULB_SESSION_SCOPE='aaaaaaaa"));
        assert!(s.contains("LIGHTBULB_SESSION_LAUNCH_PID=$$ '/bin/claude' '--' '$(rm -rf ~); echo '\\''hi'\\''\nnext'"));
    }

    #[test]
    fn session_start_rejects_unknown_fields_modes_and_unapproved_folders() {
        let home = tempfile::tempdir().unwrap();
        // No CLI installed under this home and no PATH hit is assumed for "bash".
        let bad = [
            json!({"tool":"bash","folder":"/tmp","model":null,"mode":null,"prompt":"","startId":"s"}),
            json!({"tool":"claude","folder":"/tmp","model":null,"mode":null,"prompt":"","startId":"s","argv":["sh"]}),
            json!({"tool":"claude","folder":"/tmp","model":null,"mode":null,"prompt":"","startId":"../x"}),
        ];
        for params in bad {
            assert!(validate(params, home.path()).is_err());
        }
        assert!(!listed(
            &pairs(CLAUDE_MODES),
            &Some("bypassPermissions".into())
        ));
        assert!(!listed(&pairs(CODEX_MODES), &Some("never".into())));
        assert!(!listed(&pairs(CODEX_EFFORTS), &Some("ultra".into())));
        assert!(listed(&pairs(CLAUDE_MODES), &None));
        assert!(!valid_model("--oss"));
        let subcommand = json!({"tool":"claude","folder":"/tmp","model":null,"mode":null,"prompt":" mcp ","startId":"s"});
        assert_eq!(
            validate(subcommand, home.path()).err().unwrap().0,
            "INVALID_REQUEST"
        );
    }

    #[test]
    fn session_start_codex_models_come_from_its_own_cache() {
        let home = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(home.path().join(".codex")).unwrap();
        std::fs::write(
            home.path().join(".codex/models_cache.json"),
            r#"{"models":[{"slug":"gpt-x","display_name":"GPT-X","visibility":"list"},
                {"slug":"hidden","visibility":"hide"},{"slug":"--bad","visibility":"list"}]}"#,
        )
        .unwrap();
        assert_eq!(
            codex_models(home.path()),
            vec![json!({"id":"gpt-x","label":"GPT-X"})]
        );
    }

    #[test]
    fn session_start_launches_without_a_native_approval() {
        // Owner-only end to end (see the module doc): the start never opens the
        // approve-once window. Spelled in two halves so this test is not a match.
        let source = include_str!("session_start.rs");
        assert!(!source.contains(concat!("desktop_approval", "::prompt")));
    }

    #[test]
    fn session_start_launch_failure_says_what_failed() {
        let (code, message) = launch(
            Path::new("/dev/null/session-launch"),
            Path::new("/tmp"),
            &["/bin/echo".into()],
            "s-1",
        )
        .unwrap_err();
        assert_eq!(code, "LAUNCH_FAILED");
        assert!(message.starts_with("Terminal didn't open: "), "{message}");
    }

    #[test]
    fn session_start_script_runs_in_the_folder_and_deletes_itself() {
        // Dry run of what Terminal executes, with /bin/echo standing in for the CLI.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("s-1.command");
        let argv = ["/bin/echo".to_string(), "--".into(), "hi $(whoami)".into()];
        std::fs::write(&path, script(dir.path(), &path, &argv)).unwrap();
        let out = std::process::Command::new("/bin/zsh")
            .arg(&path)
            .output()
            .unwrap();
        assert!(out.status.success());
        assert_eq!(String::from_utf8_lossy(&out.stdout), "-- hi $(whoami)\n");
        assert!(!path.exists());
    }

    #[test]
    fn held_start_runs_the_provider_in_place_with_its_start_link() {
        let argv = vec!["/opt/claude".to_string(), "--permission-mode".into(), "manual".into(), "--".into(), "fix it".into()];
        assert_eq!(held_argv(&argv, None), argv);
        let held = held_argv(&argv, Some(("start-1", "/scope")));
        assert_eq!(&held[..2], &["/bin/sh", "-c"]);
        assert!(held[2].contains("LAUNCH_PID=$$") && held[2].ends_with(r#"exec "$@""#));
        assert_eq!(&held[3..6], &["lightbulb-start", "start-1", "/scope"]);
        assert_eq!(&held[6..], &argv[..]);
    }
}
