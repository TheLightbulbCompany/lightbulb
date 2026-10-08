//! Start a Claude Code / Codex session on this Mac from Lightbulb (owner only).
//!
//! The backend only proves the request is from the human who paired this Mac. Here
//! every field is re-validated against this Mac's own truth: the tool must be an
//! installed fixed binary, the folder one of the approved folders (Settings → Desktop →
//! Approved folders), model/mode from a closed list, and the prompt is passed after `--` so it
//! can never become a flag. Then a held session opens on this Mac so the owner sees it;
//! sharing must already be on. Every session is tracked; Lightbulb starts its own in a
//! holder so they have a live terminal.
//!
//! No native approve-once window: `session.start.v1` is owner-only end to end. The
//! backend resolves it only to the caller's OWN Mac (the ready row and the reciprocal
//! pairing must both name the Clerk user asking, and the command is granted only to
//! that pairing), so the requester is always the person at this Mac, starting a
//! session he just typed. What reaches the CLI is a fixed installed binary in an
//! approved folder, with no permission-bypass mode, opened visibly on this Mac where
//! the CLI's own approvals still apply. A second "are you sure?" for your own click
//! added nothing. A teammate cannot start sessions here at all.
use serde::Deserialize;
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
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

pub(crate) fn tools(home: &Path) -> Vec<Value> {
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
        "cli": crate::cli::status(),
    })
}

fn listed(options: &[Value], value: &Option<String>) -> bool {
    value
        .as_deref()
        .map_or(true, |v| options.iter().any(|o| o["id"] == v))
}

/// The exact argv the holder runs: fixed binary, closed flags, prompt after `--`.
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
        .filter(|f| approved_now().contains(f))
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

pub async fn start(
    app: tauri::AppHandle,
    params: Value,
    cancel: watch::Receiver<bool>,
) -> Result<Value, Failure> {
    let result = async {
        let (start, folder, argv, scope) = prepare(&app, params, &cancel)?;
        let argv = held_argv(&argv, Some((start.start_id.as_str(), scope.as_str())));
        let place = folder.file_name().map_or_else(|| folder.display().to_string(), |n| n.to_string_lossy().into_owned());
        start_held(folder, argv)
            .await
            .map_err(|e| ("LAUNCH_FAILED", format!("The session didn't start: {e}")))?;
        // Started from elsewhere with no window: tell the owner at this Mac (spec §5.2).
        let tool = if start.tool == "codex" { "Codex" } else { "Claude Code" };
        let _ = crate::notify::native_notify(
            format!("{tool} started in {place}"),
            Some("End it from its panel in Lightbulb.".into()),
            Some(format!("start-{}", start.start_id)),
            None,
            None,
        )
        .await;
        Ok(json!({"started": true, "tool": start.tool}))
    }
    .await;
    match &result {
        Ok(v) => crate::log(&format!("[session-start] launched {}", v["tool"])),
        Err((code, message)) => crate::log(&format!("[session-start] failed {code}: {message}")),
    }
    result
}

fn prepare(
    app: &tauri::AppHandle,
    params: Value,
    cancel: &watch::Receiver<bool>,
) -> Result<(Start, PathBuf, Vec<String>, String), Failure> {
    let (start, folder, argv) = validate(params, &home())?;
    crate::log(&format!("[session-start] {} {} in {}", start.start_id, start.tool, folder.display()));
    if !crate::local_sessions::enrollment::sharing_on(app) {
        return Err(("SHARING_OFF", "Turn on session sharing in the Lightbulb app on this Mac first.".into()));
    }
    if *cancel.borrow() {
        return Err(("CANCELLED", "The start was cancelled.".into()));
    }
    let scope = crate::local_sessions::enrollment::launch_scope(app)
        .map_err(|_| ("SHARING_OFF", "Turn on session sharing in Lightbulb on this Mac first.".to_string()))?;
    Ok((start, folder, argv, scope))
}

/// Start `argv` in a new holder in `folder`: the one launcher for the New session view, a remote
/// start, a pick-up and a resume. It shows in the app's session panel when a person opens it.
/// Every session is tracked; one started here runs in a holder, so it has a live terminal.
pub(crate) async fn start_held(
    folder: PathBuf,
    argv: Vec<String>,
) -> Result<crate::holder::manager::HolderInfo, String> {
    // No window: the session panel attaches when a person opens the session (holder::panel).
    tauri::async_runtime::spawn_blocking(move || crate::holder::manager::start(&folder, &argv))
        .await
        .map_err(|e| e.to_string())?
}

/// The argv a holder runs for a start. With a start link, `/bin/sh` exports the link with its own
/// pid and then execs the provider in place, so the launch pid is the provider's pid.
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
/// tool, approved folder, closed model and mode lists, prompt after `--`), then a held, linked
/// session. The page shows it in its panel at once (by holder id) and as a session once it reports.
#[tauri::command]
pub async fn terminal_start(app: tauri::AppHandle, params: Value) -> Result<Value, String> {
    let (start, folder, argv) = validate(params, &home()).map_err(|(_, message)| message)?;
    // Sessions started here are shared (ruling 8): without sharing there is nowhere to show it.
    let scope = crate::local_sessions::enrollment::launch_scope(&app)
        .map_err(|_| "Turn on session sharing in Lightbulb on this Mac first.".to_string())?;
    let argv = held_argv(&argv, Some((start.start_id.as_str(), scope.as_str())));
    let info = start_held(folder, argv).await?;
    crate::log(&format!("[session-start] held {} {}", start.start_id, start.tool));
    Ok(json!({"holderId": info.id, "tool": start.tool}))
}

/// Whether `folder` (canonical) is one of the approved folders or anywhere inside one: the
/// person running `lightbulb` is at this Mac and already in that folder. A view or a remote
/// start still names an approved folder exactly (`validate`).
/// The approved folders as they are now, canonical: what a start from a terminal (and a resume)
/// must be inside.
pub(crate) fn approved_now() -> Vec<PathBuf> {
    // Only a folder that is still the one approved (`root_unchanged`, the file-access check):
    // a stored path that now resolves elsewhere approves nothing.
    approved_folders()
        .into_iter()
        .map(PathBuf::from)
        .filter(|root| crate::file_access::root_unchanged(root))
        .collect()
}

pub(crate) fn inside_approved(folder: &Path, approved: &[PathBuf]) -> bool {
    approved.iter().any(|a| crate::file_access::is_inside(folder, a))
}

/// The provider flags that point it at another folder: Codex `-C`/`--cd` (its working root) and
/// `--add-dir` (writable folders), Claude `--add-dir` (folders it may use; it takes several).
fn path_flags(tool: &str) -> &'static [&'static str] {
    if tool == "codex" { &["-C", "--cd", "--add-dir"] } else { &["--add-dir"] }
}

/// The refusal when a path-bearing flag names a folder outside every approved folder. Each path
/// is taken relative to `cwd`, made canonical and held to the same rule as the folder the command
/// runs in. Both `--flag value` and `--flag=value` (and `-Cvalue`), repeated or not; Claude's
/// `--add-dir` takes every following word up to the next flag. After `--` is prompt text.
fn unapproved_path_flag(tool: &str, args: &[String], cwd: &Path, approved: &[PathBuf]) -> Option<String> {
    let flags = path_flags(tool);
    let mut paths: Vec<(&str, &str)> = vec![];
    let mut i = 0;
    while i < args.len() && args[i] != "--" {
        let arg = args[i].as_str();
        if let Some(flag) = flags.iter().find(|f| arg == **f) {
            if tool == "claude" {
                while i + 1 < args.len() && !args[i + 1].starts_with('-') {
                    paths.push((flag, &args[i + 1]));
                    i += 1;
                }
            } else if let Some(value) = args.get(i + 1) {
                paths.push((flag, value));
                i += 1;
            }
        } else if let Some((flag, value)) = flags.iter().find_map(|f| {
            arg.strip_prefix(&format!("{f}="))
                .or_else(|| (f.len() == 2).then(|| arg.strip_prefix(*f)).flatten())
                .filter(|v| !v.is_empty())
                .map(|v| (*f, v))
        }) {
            paths.push((flag, value));
        }
        i += 1;
    }
    paths.into_iter().find_map(|(flag, path)| {
        let ok = cwd
            .join(path)
            .to_str()
            .and_then(|p| crate::file_access::canonicalize(p).ok())
            .is_some_and(|p| inside_approved(&p, approved));
        (!ok).then(|| format!(
            "{flag} {path} is not inside a folder you approved for Lightbulb. Add it in the Lightbulb app under Settings → Desktop → Approved folders, then run this again."
        ))
    })
}

/// The `lightbulb` command's argv: the person's own arguments, unchanged. Codex also gets
/// `--no-daemon`, which keeps it a dedicated writer so its start hook links to this start.
fn cli_argv(program: &Path, tool: &str, args: &[String]) -> Vec<String> {
    let mut argv = vec![program.to_string_lossy().into_owned()];
    if tool == "codex" {
        argv.push("--no-daemon".into());
    }
    argv.extend(args.iter().cloned());
    argv
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct CliStart {
    tool: String,
    folder: String,
    args: Vec<String>,
    start_id: String,
}

/// `lightbulb claude|codex` in a terminal (`cli.rs`): the tool must be installed, the folder an
/// approved folder or inside one, and sharing on. No window, because the terminal that asked
/// attaches itself. Never starts a session that would not be shared.
/// `env` is the calling terminal's environment (the provider gets it instead of the login
/// shell's); `approve_folder` is the person's "yes" in that terminal to adding the folder to the
/// approved folders.
pub(crate) fn cli_start(
    app: &tauri::AppHandle,
    params: Value,
    size: (u16, u16),
    env: Option<Vec<(String, String)>>,
    approve_folder: bool,
) -> Result<crate::holder::manager::HolderInfo, String> {
    let start: CliStart = serde_json::from_value(params).map_err(|_| "Malformed request.".to_string())?;
    if let Some(reason) = crate::cli::refusal(&start.tool, &start.args) {
        return Err(reason);
    }
    if start.args.iter().any(|a| a.contains('\0')) || start.args.iter().map(String::len).sum::<usize>() > 64 << 10
        || start.start_id.is_empty() || start.start_id.len() > 64
        || !start.start_id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err("Malformed request.".into());
    }
    let scope = crate::local_sessions::enrollment::launch_scope(app).map_err(|_| {
        "Session sharing is off in Lightbulb on this Mac. Turn it on in Settings → Desktop → Session sharing, then run this again.".to_string()
    })?;
    let home = home();
    let program = tools(&home)
        .iter()
        .any(|t| t["id"] == start.tool.as_str())
        .then(|| crate::local_sessions::provider_program(&home, &start.tool))
        .flatten()
        .ok_or(format!("{} is not installed on this Mac.", start.tool))?;
    if start.tool == "codex" {
        // The installed Codex's own command list: a subcommand added after this build still refuses.
        let installed = crate::cli::help_commands(&codex_help(&program));
        if let Some(reason) = crate::cli::codex_refusal(&start.args, &installed) {
            return Err(reason);
        }
    }
    if approve_folder && Path::new(&start.folder).is_dir() {
        // The same store Settings → Desktop → Approved folders writes.
        crate::file_access::add_path(&start.folder).map_err(|e| e.to_string())?;
    }
    let approved = approved_now();
    let folder = crate::file_access::canonicalize(&start.folder)
        .ok()
        .filter(|f| f.is_dir() && inside_approved(f, &approved))
        .ok_or(crate::cli::FOLDER_NOT_APPROVED.to_string())?;
    if let Some(reason) = unapproved_path_flag(&start.tool, &start.args, &folder, &approved) {
        return Err(reason);
    }
    let argv = held_argv(&cli_argv(&program, &start.tool, &start.args), Some((start.start_id.as_str(), scope.as_str())));
    let info = crate::holder::manager::start_sized_env(&folder, &argv, Some(size), env)?;
    crate::log(&format!("[session-start] terminal command {} {} in {}", start.start_id, start.tool, folder.display()));
    Ok(info)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_path_flags_must_name_approved_folders() {
        let approved_dir = tempfile::tempdir().unwrap();
        let root = crate::file_access::canonicalize(approved_dir.path().to_str().unwrap()).unwrap();
        std::fs::create_dir(root.join("lib")).unwrap();
        let outside = tempfile::tempdir().unwrap();
        let outside = outside.path().to_str().unwrap().to_owned();
        let approved = vec![root.clone()];
        let args = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        // An approved --add-dir passes, also relative to the folder the command runs in.
        assert_eq!(unapproved_path_flag("claude", &args(&["--add-dir", "lib"]), &root, &approved), None);
        assert_eq!(unapproved_path_flag("codex", &args(&["--add-dir", root.to_str().unwrap()]), &root, &approved), None);
        // An unapproved -C, --add-dir=…, -C<path> and a second --add-dir are refused.
        let refused = unapproved_path_flag("codex", &args(&["-C", &outside]), &root, &approved).unwrap();
        assert!(refused.contains(&outside), "{refused}");
        assert!(unapproved_path_flag("codex", &args(&[&format!("--add-dir={outside}")]), &root, &approved).is_some());
        assert!(unapproved_path_flag("codex", &args(&[&format!("-C{outside}")]), &root, &approved).is_some());
        assert!(unapproved_path_flag("claude", &args(&["--add-dir", "lib", &outside]), &root, &approved).is_some());
        assert!(unapproved_path_flag("claude", &args(&["--add-dir", "lib", "--add-dir", &outside]), &root, &approved).is_some());
        assert!(unapproved_path_flag("codex", &args(&["--cd", "../.."]), &root, &approved).is_some());
        // After `--` it is prompt text, never checked; other flags are not paths.
        assert_eq!(unapproved_path_flag("codex", &args(&["--", "-C", &outside]), &root, &approved), None);
        assert_eq!(unapproved_path_flag("claude", &args(&["--model", "opus", "--", "--add-dir", &outside]), &root, &approved), None);
        // Claude has no -C flag of its own.
        assert_eq!(unapproved_path_flag("claude", &args(&["-C", &outside]), &root, &approved), None);
    }

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
    fn held_start_runs_the_provider_in_place_with_its_start_link() {
        let argv = vec!["/opt/claude".to_string(), "--permission-mode".into(), "manual".into(), "--".into(), "fix it".into()];
        assert_eq!(held_argv(&argv, None), argv);
        let held = held_argv(&argv, Some(("start-1", "/scope")));
        assert_eq!(&held[..2], &["/bin/sh", "-c"]);
        assert!(held[2].contains("LAUNCH_PID=$$") && held[2].ends_with(r#"exec "$@""#));
        assert_eq!(&held[3..6], &["lightbulb-start", "start-1", "/scope"]);
        assert_eq!(&held[6..], &argv[..]);
    }

    /// Owner ruling 2026-10-07: New session, a remote start and `lightbulb claude` all hand the
    /// holder an argv it knows as Claude Code, so it runs in the classic renderer; Codex does not.
    #[test]
    fn every_start_of_claude_code_gets_the_classic_renderer_and_codex_does_not() {
        let classic = |argv: &[String]| {
            crate::holder::run::provider_environment(vec![], argv).iter().any(|(k, v)| k == crate::holder::run::CLASSIC_RENDERER && v == "1")
        };
        let start = |tool: &str| -> Start {
            serde_json::from_value(json!({"tool": tool, "folder": "/f", "model": null, "mode": null, "effort": null, "prompt": "fix it", "startId": "start-1"})).unwrap()
        };
        let link = Some(("start-1", "/scope"));
        let claude = Path::new("/Users/a/.local/share/claude/versions/2.1.293");
        let codex = Path::new("/opt/homebrew/bin/codex");
        // New session and a remote start.
        assert!(classic(&held_argv(&argv(claude, &start("claude")), link)));
        assert!(!classic(&held_argv(&argv(codex, &start("codex")), link)));
        // `lightbulb claude …` (the shell switch routes a plain `claude` here), whatever the person adds.
        for args in [&[][..], &["--resume", "abc"], &["attach", "8ed71738"], &["--", "codex"]] {
            let args: Vec<String> = args.iter().map(|s| s.to_string()).collect();
            assert!(classic(&held_argv(&cli_argv(claude, "claude", &args), link)), "{args:?}");
            assert!(!classic(&held_argv(&cli_argv(codex, "codex", &args), link)), "{args:?}");
        }
    }

    #[test]
    fn session_start_terminal_command_folder_is_an_approved_folder_or_inside_one() {
        let approved = [PathBuf::from("/Users/a/code")];
        assert!(inside_approved(Path::new("/Users/a/code"), &approved));
        assert!(inside_approved(Path::new("/Users/a/code/app"), &approved));
        assert!(!inside_approved(Path::new("/Users/a/codex"), &approved), "by component, not by prefix");
        assert!(!inside_approved(Path::new("/Users/a"), &approved), "never above an approved folder");
    }

    #[test]
    fn session_start_terminal_command_passes_the_person_s_arguments_unchanged() {
        let args: Vec<String> = ["--resume", "abc", "--dangerously-skip-permissions"].iter().map(|s| s.to_string()).collect();
        assert_eq!(cli_argv(Path::new("/bin/claude"), "claude", &args), ["/bin/claude", "--resume", "abc", "--dangerously-skip-permissions"]);
        // Codex: only the plumbing the start link needs, then the person's words.
        assert_eq!(cli_argv(Path::new("/bin/codex"), "codex", &args[..2]), ["/bin/codex", "--no-daemon", "--resume", "abc"]);
    }
}
