//! The `lightbulb` command (spec §5.3): `lightbulb claude|codex [any provider arguments]` asks
//! the running app to start a held, shared session in this folder, says who it is shared with,
//! then is that session's terminal.
//!
//! The app does the start (`session_start::cli_start`): the installed tool, the approved
//! folders (this folder or one it is inside), and sharing must be on. The provider's arguments
//! pass through unchanged (owner ruling 2026-10-03); only the forms with no terminal to share
//! are refused. This process only turns words into that request and moves bytes.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
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

use crate::holder::model::strip_queries;
use crate::holder::protocol::{encode, read_frame, split_offset, DETACHED, EXIT, HELLO, INPUT, OUTPUT, RESIZE, SNAPSHOT};
use std::sync::atomic::{AtomicI32, Ordering};
use std::sync::OnceLock;

/// The command's words when this binary runs as `lightbulb …` (the `~/.local/bin` link) or as
/// `lightbulb-desktop cli …`.
pub fn cli_args(argv: &[String]) -> Option<&[String]> {
    if argv.first().and_then(|a| Path::new(a).file_name()).is_some_and(|n| n == "lightbulb") {
        return Some(&argv[1..]);
    }
    (argv.get(1).map(String::as_str) == Some("cli")).then(|| &argv[2..])
}

#[derive(Debug, PartialEq)]
pub struct Ask {
    pub tool: String,
    /// The provider's arguments, passed through unchanged.
    pub args: Vec<String>,
}

const USAGE: &str = "Usage: lightbulb claude|codex [any claude or codex arguments]\n\
Starts a session shared with your Lightbulb workspace. Plain `claude` or `codex` stays private.";

pub fn parse(args: &[String]) -> Result<Ask, String> {
    let Some((tool, rest)) = args.split_first().filter(|(t, _)| *t == "claude" || *t == "codex") else {
        return Err(USAGE.into());
    };
    if let Some(reason) = refusal(tool, rest) {
        return Err(reason);
    }
    Ok(Ask { tool: tool.clone(), args: rest.to_vec() })
}

/// Exactly `session_start::cli_start`'s fields, plus this terminal's size.
pub fn request(ask: &Ask, folder: &Path, (cols, rows): (u16, u16)) -> Value {
    json!({
        "start": {"tool": ask.tool, "folder": folder.to_string_lossy(), "args": ask.args,
            "startId": uuid::Uuid::new_v4().to_string()},
        "cols": cols, "rows": rows,
    })
}

/// The workspace name comes from the web page: never let it write escape sequences here.
pub fn shared_line(workspace: Option<&str>, tool: &str) -> String {
    let name: String = workspace.unwrap_or("").chars().filter(|c| !c.is_control()).take(80).collect();
    let name = match name.trim() {
        "" => "your Lightbulb workspace",
        name => name,
    };
    format!("● Shared with {name}. Run plain `{tool}` to keep a session private.")
}

#[derive(Debug, PartialEq)]
pub enum Outcome {
    /// The provider exited with this code.
    Exited(i32),
    /// Another terminal (the app's panel) took the keyboard; the session keeps running.
    Moved,
    /// The connection ended without either.
    Lost,
}

/// Holder frames → this terminal, until the session exits, moves, or the connection drops.
/// Also returns whether the provider left this terminal on its alternate screen.
pub fn pump(holder: &mut impl Read, out: &mut impl Write) -> (Outcome, bool) {
    // Only to know whether the alternate screen is on when this ends.
    let mut screen = vt100::Parser::new(24, 80, 0);
    let outcome = loop {
        let Ok((kind, payload)) = read_frame(holder) else { break Outcome::Lost };
        match kind {
            OUTPUT | SNAPSHOT => {
                let Some((_, bytes)) = split_offset(&payload) else { continue };
                let bytes = strip_queries(bytes);
                screen.process(&bytes);
                if out.write_all(&bytes).and_then(|()| out.flush()).is_err() {
                    break Outcome::Lost;
                }
            }
            EXIT => {
                let code = serde_json::from_slice::<Value>(&payload).ok().and_then(|v| v["code"].as_i64()).unwrap_or(1);
                break Outcome::Exited(code as i32);
            }
            DETACHED => break Outcome::Moved,
            _ => {}
        }
    };
    (outcome, screen.screen().alternate_screen())
}

/// Hand the terminal back as a shell expects it: no mouse, focus or paste reporting, a visible
/// cursor, plain colours, and the main screen if the provider left the alternate one on.
pub fn reset_sequence(alternate_screen: bool) -> Vec<u8> {
    let mut out = if alternate_screen { b"\x1b[?1049l".to_vec() } else { vec![] };
    out.extend_from_slice(b"\x1b[?2004l\x1b[?1004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?25h\x1b[0m");
    out
}

/// The terminal settings before raw mode, restored on every way out (and from a panic).
static SAVED: OnceLock<libc::termios> = OnceLock::new();
/// The signal that ended the attachment, 0 if none.
static STOPPED_BY: AtomicI32 = AtomicI32::new(0);

fn window_size() -> (u16, u16) {
    let mut ws: libc::winsize = unsafe { std::mem::zeroed() };
    if unsafe { libc::ioctl(0, libc::TIOCGWINSZ, &mut ws) } == 0 && ws.ws_col > 0 && ws.ws_row > 0 {
        (ws.ws_col, ws.ws_row)
    } else {
        (80, 24)
    }
}

fn raw_mode() {
    let mut t: libc::termios = unsafe { std::mem::zeroed() };
    if unsafe { libc::tcgetattr(0, &mut t) } == 0 && SAVED.set(t).is_ok() {
        unsafe {
            libc::cfmakeraw(&mut t);
            libc::tcsetattr(0, libc::TCSANOW, &t);
        }
    }
}

fn restore() {
    if let Some(t) = SAVED.get() {
        unsafe { libc::tcsetattr(0, libc::TCSANOW, t) };
    }
}

/// SIGWINCH resizes the session; SIGINT, SIGTERM and SIGHUP (the terminal closed) detach and
/// leave it running. Blocked before any thread starts, so only `sigwait` receives them.
fn block_signals() -> libc::sigset_t {
    unsafe {
        let mut set: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut set);
        for sig in [libc::SIGWINCH, libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            libc::sigaddset(&mut set, sig);
        }
        libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
        set
    }
}

fn attach(socket: &Path, (cols, rows): (u16, u16)) -> i32 {
    let signals = block_signals();
    let holder = match UnixStream::connect(socket) {
        Ok(holder) if same_user(&holder) => holder,
        _ => {
            eprintln!("The session ended before it could be shown.");
            return 1;
        }
    };
    let (Ok(mut reader), Ok(stopper), Ok(writer)) = (holder.try_clone(), holder.try_clone(), holder.try_clone()) else {
        return 1;
    };
    let writer = Arc::new(Mutex::new(writer));
    // All output from the start rather than a snapshot: it draws under the line printed above,
    // as the provider run directly would.
    let hello = json!({"role": "interactive", "cols": cols, "rows": rows, "replayFrom": 0});
    if writer.lock().unwrap().write_all(&encode(HELLO, hello.to_string().as_bytes())).is_err() {
        return 1;
    }
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore();
        default_hook(info);
    }));
    raw_mode();
    let resize = writer.clone();
    std::thread::spawn(move || loop {
        let mut sig = 0;
        if unsafe { libc::sigwait(&signals, &mut sig) } != 0 {
            continue;
        }
        if sig == libc::SIGWINCH {
            let (cols, rows) = window_size();
            let mut size = cols.to_be_bytes().to_vec();
            size.extend_from_slice(&rows.to_be_bytes());
            let _ = resize.lock().unwrap().write_all(&encode(RESIZE, &size));
        } else {
            STOPPED_BY.store(sig, Ordering::SeqCst);
            let _ = stopper.shutdown(std::net::Shutdown::Both);
            return;
        }
    });
    std::thread::spawn(move || {
        let mut buf = [0u8; 65536];
        let mut stdin = std::io::stdin();
        while let Ok(n) = stdin.read(&mut buf) {
            if n == 0 || writer.lock().unwrap().write_all(&encode(INPUT, &buf[..n])).is_err() {
                return;
            }
        }
    });
    let (outcome, alternate) = pump(&mut reader, &mut std::io::stdout());
    restore();
    // Writes may fail once the terminal is gone (SIGHUP); never panic over that.
    let mut out = std::io::stdout();
    let _ = out.write_all(&reset_sequence(alternate));
    let (code, line) = match outcome {
        Outcome::Exited(code) => (code, None),
        Outcome::Moved => (0, Some("● This session moved to the Lightbulb app. It keeps running there.")),
        Outcome::Lost => match STOPPED_BY.load(Ordering::SeqCst) {
            0 => (1, Some("● Lost this session's terminal. If it is still running, it is in the Lightbulb app.")),
            sig => (128 + sig, Some("● Detached. The session keeps running in the Lightbulb app.")),
        },
    };
    if let Some(line) = line {
        let _ = writeln!(out, "\n{line}");
    }
    let _ = out.flush();
    code
}

/// `lightbulb …`: the process exit code.
pub fn main(args: &[String]) -> i32 {
    let ask = match parse(args) {
        Ok(ask) => ask,
        Err(message) => {
            eprintln!("{message}");
            return 2;
        }
    };
    // Before asking the app: never start a session nobody here can see.
    if unsafe { libc::isatty(0) != 1 || libc::isatty(1) != 1 } {
        eprintln!("lightbulb {} needs a terminal: run it in Terminal, VS Code or another terminal app.", ask.tool);
        return 1;
    }
    let Ok(folder) = std::env::current_dir() else {
        eprintln!("lightbulb: this folder no longer exists.");
        return 1;
    };
    let size = window_size();
    match ask_app(&app_socket(), &request(&ask, &folder, size)) {
        Ok(started) => {
            println!("{}", shared_line(started.workspace.as_deref(), &ask.tool));
            attach(&started.socket, size)
        }
        Err(message) => {
            eprintln!("{message}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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

    use crate::holder::protocol::{encode, with_offset, DETACHED, EXIT, HELLO, OUTPUT};

    #[test]
    fn cli_runs_as_lightbulb_or_as_the_cli_subcommand_only() {
        let link = words(&["/Users/a/.local/bin/lightbulb", "claude", "fix it"]);
        assert_eq!(cli_args(&link).unwrap(), &link[1..]);
        let sub = words(&["/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop", "cli", "codex"]);
        assert_eq!(cli_args(&sub).unwrap(), &sub[2..]);
        assert_eq!(cli_args(&words(&["/x/lightbulb-desktop", "holder", "/s", "--", "cat"])), None);
        assert_eq!(cli_args(&words(&["/x/lightbulb-desktop"])), None);
    }

    #[test]
    fn cli_passes_the_provider_arguments_through_and_refuses_only_non_interactive_forms() {
        assert_eq!(parse(&words(&["claude"])).unwrap(), Ask { tool: "claude".into(), args: vec![] });
        let ask = parse(&words(&["claude", "--resume", "abc", "--dangerously-skip-permissions", "fix it"])).unwrap();
        assert_eq!(ask.args, words(&["--resume", "abc", "--dangerously-skip-permissions", "fix it"]), "unchanged");
        assert_eq!(parse(&words(&["codex", "resume"])).unwrap().args, words(&["resume"]));
        for refused in [&["claude", "-p", "hi"][..], &["claude", "--print"], &["codex", "exec", "hi"], &["bash"], &[], &["--help"]] {
            assert!(parse(&words(refused)).is_err(), "{refused:?}");
        }
        let request = request(&ask, Path::new("/Users/a/code/app"), (80, 24));
        assert_eq!(request["start"]["tool"], "claude");
        assert_eq!(request["start"]["folder"], "/Users/a/code/app");
        assert_eq!(request["start"]["args"], json!(["--resume", "abc", "--dangerously-skip-permissions", "fix it"]));
        assert_eq!((request["cols"].as_u64(), request["rows"].as_u64()), (Some(80), Some(24)));
        let keys: Vec<_> = request["start"].as_object().unwrap().keys().cloned().collect();
        assert_eq!(keys, ["tool", "folder", "args", "startId"], "exactly cli_start's fields");
    }

    #[test]
    fn cli_shared_line_names_the_workspace_and_never_prints_its_control_bytes() {
        assert_eq!(shared_line(Some("Acme"), "claude"), "● Shared with Acme. Run plain `claude` to keep a session private.");
        assert_eq!(shared_line(Some("Ev\x1b]0;pwned\x07il"), "codex"), "● Shared with Ev]0;pwnedil. Run plain `codex` to keep a session private.");
        assert_eq!(shared_line(None, "claude"), "● Shared with your Lightbulb workspace. Run plain `claude` to keep a session private.");
    }

    #[test]
    fn cli_pump_draws_output_without_queries_and_reports_how_the_session_left() {
        let mut frames = encode(HELLO, b"{}");
        frames.extend(encode(OUTPUT, &with_offset(0, b"\x1b[?1049hhi\x1b[c")));
        frames.extend(encode(DETACHED, b""));
        let mut out = vec![];
        assert_eq!(pump(&mut frames.as_slice(), &mut out), (Outcome::Moved, true));
        assert_eq!(out, b"\x1b[?1049hhi");

        let mut frames = encode(OUTPUT, &with_offset(0, b"bye"));
        frames.extend(encode(EXIT, br#"{"status":"ExitStatus { code: 3, signal: None }","code":3}"#));
        assert_eq!(pump(&mut frames.as_slice(), &mut vec![]), (Outcome::Exited(3), false));
        assert_eq!(pump(&mut &b""[..], &mut vec![]), (Outcome::Lost, false));
    }

    #[test]
    fn cli_reset_leaves_the_alternate_screen_only_when_on_it() {
        assert!(reset_sequence(true).starts_with(b"\x1b[?1049l"));
        assert!(!reset_sequence(false).windows(8).any(|w| w == b"\x1b[?1049l"), "would jump the cursor to a stale saved position");
        assert!(reset_sequence(false).ends_with(b"\x1b[?25h\x1b[0m"));
    }
}
