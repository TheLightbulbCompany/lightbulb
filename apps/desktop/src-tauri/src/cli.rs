//! The `lightbulb` command (spec §5.3): `lightbulb claude|codex [any provider arguments]` asks
//! the running app to start a held, shared session in this folder, says who it is shared with,
//! then is that session's terminal.
//!
//! The app does the start (`session_start::cli_start`): the installed tool, the approved
//! folders (this folder or one it is inside), and sharing must be on. The provider's arguments
//! pass through unchanged (owner ruling 2026-10-03); only the forms with no terminal to share
//! are refused. This process only turns words into that request and moves bytes.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// Codex subcommands that run without the interactive terminal (`resume` and `fork` are TUIs).
const CODEX_NON_TUI: &[&str] = &[
    "exec", "e", "review", "login", "logout", "mcp", "mcp-server", "app-server", "app",
    "completion", "sandbox", "debug", "apply", "a", "cloud", "features", "help",
];

/// Why these arguments cannot be a shared session, if they cannot: the non-interactive forms
/// have no terminal to share. Everything after `--` is the provider's own text, never checked.
// ponytail: a Codex flag whose VALUE is exactly a subcommand name (`--profile review`) is refused
// too; a per-flag value table would fix it if anyone hits it.
pub fn refusal(tool: &str, args: &[String]) -> Option<String> {
    let before_dashes = args.iter().take_while(|a| *a != "--");
    for arg in before_dashes {
        let refused = match tool {
            "claude" => {
                arg == "--print"
                    || arg.starts_with("--print=")
                    || (arg.len() > 1 && arg.starts_with('-') && !arg.starts_with("--") && arg[1..].bytes().all(|b| b.is_ascii_alphabetic()) && arg.contains('p'))
            }
            "codex" => CODEX_NON_TUI.contains(&arg.as_str()),
            _ => false,
        };
        if refused {
            return Some(format!(
                "lightbulb {tool} starts an interactive shared session, and `{tool} {arg}` has no terminal to share. Run plain `{tool} {arg}` instead."
            ));
        }
    }
    None
}

/// The app's answer: the new session's holder socket, and who it is shared with.
#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Started {
    pub socket: PathBuf,
    pub workspace: Option<String>,
}

type Reply = Result<Started, String>;

const NOT_RUNNING: &str = "Lightbulb is not running. Open the Lightbulb app, then run this again.";

/// Where the app listens: beside the holders' folder, which only this Mac user can enter.
pub fn app_socket() -> PathBuf {
    // Debug builds only: the command's tests point it at their own fake app.
    #[cfg(debug_assertions)]
    if let Some(path) = std::env::var_os("LIGHTBULB_TEST_APP_SOCKET") {
        return path.into();
    }
    crate::holder::manager::socket_dir().with_file_name("cli.sock")
}

/// The other end of a local socket is this Mac user.
pub fn same_user(conn: &UnixStream) -> bool {
    let (mut uid, mut gid) = (0, 0);
    unsafe { libc::getpeereid(conn.as_raw_fd(), &mut uid, &mut gid) == 0 && uid == libc::geteuid() }
}

/// Serve the command's requests: one JSON line in, one out, from this Mac user's processes only.
pub fn serve_at(path: &Path, start: impl Fn(Value) -> Reply + Send + Sync + 'static) -> std::io::Result<()> {
    // The folder is this user's own (the caller checked it); anything left there is a socket
    // from an earlier launch.
    let _ = std::fs::remove_file(path);
    let listener = UnixListener::bind(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    let start = Arc::new(start);
    std::thread::spawn(move || {
        for conn in listener.incoming().flatten() {
            let start = start.clone();
            std::thread::spawn(move || {
                let _ = answer(conn, &*start);
            });
        }
    });
    Ok(())
}

fn answer(mut conn: UnixStream, start: &(dyn Fn(Value) -> Reply + Send + Sync)) -> std::io::Result<()> {
    if !same_user(&conn) {
        return Ok(());
    }
    conn.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut line = String::new();
    BufReader::new((&conn).take(64 << 10)).read_line(&mut line)?;
    let reply = serde_json::from_str::<Value>(&line).map_err(|_| "Malformed request.".to_string()).and_then(start);
    conn.write_all(format!("{}\n", serde_json::to_string(&reply)?).as_bytes())
}

static WORKSPACE: Mutex<Option<String>> = Mutex::new(None);

/// The workspace the app's signed-in page is in, for the "Shared with …" line.
pub fn set_workspace(name: Option<String>) {
    *WORKSPACE.lock().unwrap() = name;
}

/// The app's end, started at launch.
pub fn serve(app: tauri::AppHandle) -> Result<(), String> {
    crate::holder::manager::ensure_dir(&crate::holder::manager::socket_dir())?;
    serve_at(&app_socket(), move |request| {
        let side = |key: &str, default: u16| request[key].as_u64().and_then(|v| u16::try_from(v).ok()).unwrap_or(default);
        let size = (side("cols", 80), side("rows", 24));
        let info = crate::session_start::cli_start(&app, request["start"].clone(), size)?;
        Ok(Started { socket: info.socket, workspace: WORKSPACE.lock().unwrap().clone() })
    })
    .map_err(|e| e.to_string())
}

/// The command's end: ask the running app to start the session.
pub fn ask_app(path: &Path, request: &Value) -> Result<Started, String> {
    fn no_answer<E>(_: E) -> String {
        "Lightbulb did not answer. If a session started, it is in the Lightbulb app.".into()
    }
    let mut conn = UnixStream::connect(path).map_err(|_| NOT_RUNNING.to_string())?;
    if !same_user(&conn) {
        return Err(NOT_RUNNING.into());
    }
    // The app waits up to 10 s for a new holder to answer.
    conn.set_read_timeout(Some(Duration::from_secs(30))).map_err(no_answer)?;
    conn.write_all(format!("{request}\n").as_bytes()).map_err(no_answer)?;
    let mut line = String::new();
    BufReader::new(&conn).read_line(&mut line).map_err(no_answer)?;
    serde_json::from_str::<Reply>(&line).map_err(no_answer)?
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn words(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn cli_refuses_only_the_forms_with_no_terminal_to_share() {
        for ok in [&["--resume", "abc"][..], &["--dangerously-skip-permissions"], &["--continue"],
            &["--permission-mode", "bypassPermissions"], &["--model", "opus", "fix it"], &["--", "-p"], &[]] {
            assert_eq!(refusal("claude", &words(ok)), None, "{ok:?}");
        }
        for no in [&["-p", "hi"][..], &["--print", "hi"], &["-cp", "hi"], &["--print=json"]] {
            assert!(refusal("claude", &words(no)).is_some(), "{no:?}");
        }
        for ok in [&["resume"][..], &["fork"], &["--full-auto", "fix the build"], &["-m", "o3"], &["--", "exec"], &[]] {
            assert_eq!(refusal("codex", &words(ok)), None, "{ok:?}");
        }
        for no in [&["exec", "hi"][..], &["e", "hi"], &["review"], &["-m", "o3", "exec", "hi"], &["login"], &["mcp", "list"], &["app-server"]] {
            assert!(refusal("codex", &words(no)).is_some(), "{no:?}");
        }
    }

    #[test]
    fn cli_socket_answers_one_line_with_the_app_s_result() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cli.sock");
        serve_at(&path, |request| {
            if request["start"]["folder"] == "/ok" {
                Ok(Started { socket: "/h/ab12cd34.sock".into(), workspace: Some("Acme".into()) })
            } else {
                Err("This folder is not inside a folder you approved for Lightbulb.".into())
            }
        })
        .unwrap();
        let ok = ask_app(&path, &json!({"start": {"folder": "/ok"}})).unwrap();
        assert_eq!((ok.socket, ok.workspace.as_deref()), (PathBuf::from("/h/ab12cd34.sock"), Some("Acme")));
        assert!(ask_app(&path, &json!({"start": {"folder": "/elsewhere"}})).unwrap_err().contains("not inside"));
        assert!(ask_app(&dir.path().join("missing.sock"), &json!({})).unwrap_err().contains("not running"));
    }
}
