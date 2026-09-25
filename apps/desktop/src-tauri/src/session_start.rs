//! Start a Claude Code / Codex session on this Mac from Lightbulb (owner only).
//!
//! The backend only proves the request is from the human who paired this Mac. Here
//! every field is re-validated against this Mac's own truth: the tool must be an
//! installed fixed binary, the folder one of the approved folders (Settings → File
//! access), model/mode from a closed list, and the prompt is passed after `--` so it
//! can never become a flag. Then an exact approve-once window, then Terminal opens
//! the session so the owner sees it; sharing must already be on so it mirrors.
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use tauri::Manager;
use tokio::sync::watch;

const CLAUDE_MODELS: &[(&str, &str)] =
    &[("opus", "Opus"), ("sonnet", "Sonnet"), ("fable", "Fable")];
const CLAUDE_MODES: &[(&str, &str)] = &[("plan", "Plan"), ("acceptEdits", "Accept edits")];
// Codex's own list is {on-request, never}; "never" is not offered remotely.
const CODEX_MODES: &[(&str, &str)] = &[("on-request", "On request")];
const APPROVAL_MS: u64 = 115_000;
const MAX_PROMPT: usize = 8192;

type Failure = (&'static str, String);

#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct Start {
    tool: String,
    folder: String,
    model: Option<String>,
    mode: Option<String>,
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
            "models":pairs(CLAUDE_MODELS),"modes":pairs(CLAUDE_MODES)}),
        );
    }
    if crate::local_sessions::provider_program(home, "codex").is_some() {
        out.push(
            json!({"id":"codex","label":"Codex","modeLabel":"Approval policy",
            "models":codex_models(home),"modes":pairs(CODEX_MODES)}),
        );
    }
    out
}

fn approved_folders() -> Vec<String> {
    crate::file_access::list_paths()
        .into_iter()
        .filter(|p| Path::new(p).is_dir())
        .collect()
}

pub fn options(app: &tauri::AppHandle) -> Value {
    json!({
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
    let (model_flag, mode_flag) = if start.tool == "codex" {
        ("-m", "-a")
    } else {
        ("--model", "--permission-mode")
    };
    let mut argv = vec![program.to_string_lossy().into_owned()];
    if let Some(model) = &start.model {
        argv.extend([model_flag.into(), model.clone()]);
    }
    if let Some(mode) = &start.mode {
        argv.extend([mode_flag.into(), mode.clone()]);
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
    {
        return Err(invalid("That model or mode is not available on this Mac."));
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

fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// A private one-shot `.command` script: Terminal runs it in a login shell the owner
/// can see, it deletes itself, then execs the CLI. No shell ever parses request text
/// unquoted.
fn script(folder: &Path, path: &Path, argv: &[String]) -> String {
    format!(
        "#!/bin/zsh -l\ncd -- {} || exit 1\nrm -f -- {}\nexec {}\n",
        quote(&folder.to_string_lossy()),
        quote(&path.to_string_lossy()),
        argv.iter().map(|a| quote(a)).collect::<Vec<_>>().join(" ")
    )
}

fn launch(
    app: &tauri::AppHandle,
    folder: &Path,
    argv: &[String],
    start_id: &str,
) -> Result<(), Failure> {
    use std::io::Write;
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let failed = |_| {
        (
            "LAUNCH_FAILED",
            "Could not open Terminal on this Mac.".to_string(),
        )
    };
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|_| ("LAUNCH_FAILED", "Desktop storage unavailable.".to_string()))?
        .join("session-launch");
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&dir)
        .map_err(failed)?;
    let path = dir.join(format!("{start_id}.command"));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o700)
        .open(&path)
        .map_err(failed)?;
    file.write_all(script(folder, &path, argv).as_bytes())
        .map_err(failed)?;
    drop(file);
    let status = std::process::Command::new("/usr/bin/open")
        .args(["-a", "Terminal"])
        .arg(&path)
        .status()
        .map_err(failed)?;
    if !status.success() {
        let _ = std::fs::remove_file(&path);
        return Err((
            "LAUNCH_FAILED",
            "Could not open Terminal on this Mac.".into(),
        ));
    }
    Ok(())
}

pub async fn start(
    app: tauri::AppHandle,
    params: Value,
    cancel: watch::Receiver<bool>,
) -> Result<Value, Failure> {
    let (start, folder, argv) = validate(params, &home())?;
    if !crate::local_sessions::enrollment::sharing_on(&app) {
        return Err((
            "SHARING_OFF",
            "Turn on session sharing in the Lightbulb app on this Mac first.".into(),
        ));
    }
    let session = json!({
        "tool": start.tool,
        "folder": folder,
        "model": start.model.as_deref().unwrap_or("default"),
        "mode": start.mode.as_deref().unwrap_or("default"),
        "prompt": start.prompt,
        "command": argv,
    });
    let expires = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
        + APPROVAL_MS;
    let binding = hex::encode(Sha256::digest(
        format!("{}:{}", start.start_id, session).as_bytes(),
    ));
    let answer = crate::desktop_approval::prompt(
        app.clone(),
        json!({"binding": binding, "workspace": "this Mac", "agent": "you", "device": "this Mac",
            "run": start.start_id, "session": session, "expiresAtMs": expires}),
        cancel,
    )
    .await;
    if answer != json!({"approved": true, "binding": binding}) {
        return Err(("DENIED", "The start was not approved on the Mac.".into()));
    }
    launch(&app, &folder, &argv, &start.start_id)?;
    Ok(json!({"started": true, "tool": start.tool}))
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
        assert_eq!(codex, ["/bin/codex", "-a", "on-request"]);
    }

    #[test]
    fn session_start_script_quotes_every_argument() {
        let s = script(
            Path::new("/Users/a/it's"),
            Path::new("/tmp/x.command"),
            &[
                "/bin/claude".into(),
                "--".into(),
                "$(rm -rf ~); echo 'hi'\nnext".into(),
            ],
        );
        assert!(s.contains("cd -- '/Users/a/it'\\''s' || exit 1"));
        assert!(s.contains("exec '/bin/claude' '--' '$(rm -rf ~); echo '\\''hi'\\''\nnext'"));
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
        assert!(listed(&pairs(CLAUDE_MODES), &None));
        assert!(!valid_model("--oss"));
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
}
