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

/// Codex subcommands that open the interactive terminal; every other subcommand has none to share.
const CODEX_TUI: &[&str] = &["resume", "fork"];

/// Every Codex subcommand name and alias we know (current `codex --help` plus hidden and older
/// ones). The app also reads the installed Codex's own `--help`, so a new one cannot slip past it.
const CODEX_SUBCOMMANDS: &[&str] = &[
    "agents", "exec", "e", "review", "login", "logout", "mcp", "mcp-server", "plugin", "app-server",
    "remote-control", "app", "completion", "update", "doctor", "sandbox", "debug", "apply", "a",
    "resume", "queue", "archive", "delete", "migrate-rollouts", "unarchive", "fork", "cloud",
    "cloud-tasks", "exec-server", "execpolicy", "features", "help", "responses-api-proxy",
    "stdio-to-uds", "generate-ts", "tcp-tunnel",
];

/// Codex options that take a separate value word (`codex --help`).
const CODEX_VALUE_FLAGS: &[&str] = &[
    "-c", "--config", "--enable", "--disable", "--remote", "--remote-auth-token-env", "-i", "--image",
    "-m", "--model", "--local-provider", "-p", "--profile", "-s", "--sandbox", "-C", "--cd",
    "--add-dir", "-a", "--ask-for-approval",
];

/// The first positional word, as Codex's own parser finds it: skip options and the value word of
/// a value-taking option; `--opt=value` and `-mVALUE` carry their own value; stop at `--`.
/// Codex runs it as a subcommand when it is one, and otherwise takes it as the prompt.
// ponytail: `-i` takes several files in Codex; only the first is skipped.
pub(crate) fn first_positional(args: &[String]) -> Option<&str> {
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        if arg == "--" {
            return None;
        }
        if CODEX_VALUE_FLAGS.contains(&arg.as_str()) {
            it.next();
        } else if !arg.starts_with('-') || arg == "-" {
            return Some(arg);
        }
    }
    None
}

/// The command names and aliases under `Commands:` in a `codex --help` text. Entries are indented
/// two spaces; wrapped description lines are indented more and add only their `[aliases: ..]`.
pub(crate) fn help_commands(help: &str) -> Vec<String> {
    let mut out = vec![];
    let mut lines = help.lines().skip_while(|l| l.trim_end() != "Commands:").skip(1);
    while let Some(line) = lines.next().filter(|l| !l.trim().is_empty()) {
        if line.starts_with("  ") && !line.starts_with("   ") {
            out.extend(line.split_whitespace().next().map(str::to_string));
        }
        if let Some(rest) = line.split("[aliases:").nth(1) {
            out.extend(rest.split(']').next().unwrap_or("").split(',').map(|a| a.trim().to_string()).filter(|a| !a.is_empty()));
        }
    }
    out
}

/// The reason a Codex subcommand has no terminal to share, if the first positional is one.
/// `installed` is the installed Codex's own command list (empty when it is not known).
pub(crate) fn codex_refusal(args: &[String], installed: &[String]) -> Option<String> {
    let word = first_positional(args)?;
    let known = CODEX_SUBCOMMANDS.contains(&word) || installed.iter().any(|c| c == word);
    (known && !CODEX_TUI.contains(&word)).then(|| not_interactive("codex", word))
}

fn not_interactive(tool: &str, arg: &str) -> String {
    format!("lightbulb {tool} starts an interactive shared session, and `{tool} {arg}` has no terminal to share. Run plain `{tool} {arg}` instead.")
}

/// Why these arguments cannot be a shared session, if they cannot: the non-interactive forms
/// have no terminal to share. Everything after `--` is the provider's own text, never checked.
pub fn refusal(tool: &str, args: &[String]) -> Option<String> {
    // Informational flags print and exit: no terminal to share.
    let info: &[&str] = if tool == "codex" { &["-h", "--help", "-V", "--version"] } else { &["-h", "--help", "-v", "--version"] };
    if let Some(flag) = args.iter().take_while(|a| *a != "--").find(|a| info.contains(&a.as_str())) {
        return Some(not_interactive(tool, flag));
    }
    if tool == "codex" {
        return codex_refusal(args, &[]);
    }
    let before_dashes = args.iter().take_while(|a| *a != "--");
    for arg in before_dashes {
        let refused = tool == "claude"
            && (arg == "--print"
                || arg.starts_with("--print=")
                || (arg.len() > 1 && arg.starts_with('-') && !arg.starts_with("--") && arg[1..].bytes().all(|b| b.is_ascii_alphabetic()) && arg.contains('p')));
        if refused {
            return Some(not_interactive(tool, arg));
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
    // JSON escapes a byte to at most 6 (`\u00XX`), so 64 KiB of arguments serialize under 384 KiB plus the envelope.
    BufReader::new((&conn).take(512 << 10)).read_line(&mut line)?;
    let reply = serde_json::from_str::<Value>(&line).map_err(|_| "Malformed request.".to_string()).and_then(start);
    conn.write_all(format!("{}\n", serde_json::to_string(&reply)?).as_bytes())
}

/// (workspace id, name) the app's signed-in page is in, for the "Shared with …" line.
static WORKSPACE: Mutex<Option<(String, String)>> = Mutex::new(None);

pub fn set_workspace(workspace: Option<(String, String)>) {
    *WORKSPACE.lock().unwrap() = workspace;
}

/// The page's workspace name, only when it is the workspace this Mac shares to: a page switched
/// to another workspace must never name the wrong one (the person's only privacy signal).
pub fn shared_workspace(page: Option<&(String, String)>, enrolled_owner: Option<&str>) -> Option<String> {
    page.filter(|(id, _)| Some(id.as_str()) == enrolled_owner).map(|(_, name)| name.clone())
}

/// The app's end, started at launch.
pub fn serve(app: tauri::AppHandle) -> Result<(), String> {
    crate::holder::manager::ensure_dir(&crate::holder::manager::socket_dir())?;
    serve_at(&app_socket(), move |request| {
        let side = |key: &str, default: u16| request[key].as_u64().and_then(|v| u16::try_from(v).ok()).unwrap_or(default);
        let size = (side("cols", 80), side("rows", 24));
        let info = crate::session_start::cli_start(&app, request["start"].clone(), size)?;
        let owner = crate::local_sessions::enrollment::enrolled_owner(&app);
        let workspace = shared_workspace(WORKSPACE.lock().unwrap().as_ref(), owner.as_deref());
        Ok(Started { socket: info.socket, workspace })
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
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
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
                ALTERNATE.store(screen.screen().alternate_screen(), Ordering::SeqCst);
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
    out.extend_from_slice(b"\x1b[?2004l\x1b[?1004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l");
    // Synchronized output, the kitty keyboard stack, cursor and keypad application modes.
    // Not `\x1b[r`: resetting the scroll region moves the cursor home.
    out.extend_from_slice(b"\x1b[?2026l\x1b[<u\x1b[?1l\x1b>\x1b[?25h\x1b[0m");
    out
}

/// The terminal settings before raw mode, restored on every way out (and from a panic).
static SAVED: OnceLock<libc::termios> = OnceLock::new();
/// The signal that ended the attachment, 0 if none.
static STOPPED_BY: AtomicI32 = AtomicI32::new(0);
/// Whether the provider has the alternate screen on, for the panic hook's reset.
static ALTERNATE: AtomicBool = AtomicBool::new(false);

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

fn attach(socket: &Path, (cols, rows): (u16, u16), signals: libc::sigset_t) -> i32 {
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
        let reset = reset_sequence(ALTERNATE.load(Ordering::SeqCst));
        unsafe { libc::write(1, reset.as_ptr().cast(), reset.len()) };
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
    // First, so a Ctrl-C or SIGHUP while the app starts the session stays pending and detaches
    // with the Decision 4 line in `attach`, instead of killing this silently with the session
    // started anyway. The early exits below just exit with it pending.
    let signals = block_signals();
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
            attach(&started.socket, size, signals)
        }
        Err(message) => {
            eprintln!("{message}");
            1
        }
    }
}

/// Set once `~/.local/bin/lightbulb` is in place: the PATH line to show, if one is needed.
static INSTALLED: OnceLock<Option<String>> = OnceLock::new();

/// `~/.local/bin/lightbulb` → `exe`, in the user's own folder (no admin rights). Creates it, or
/// repairs a link to another copy of this app (moved away or not); leaves anything else alone.
pub fn install(exe: &Path, home: &Path) -> Result<(), String> {
    let bin = home.join(".local/bin");
    let link = bin.join("lightbulb");
    match std::fs::symlink_metadata(&link) {
        Ok(meta) if meta.file_type().is_symlink() => {
            let target = std::fs::read_link(&link).map_err(|e| e.to_string())?;
            if target == exe {
                return Ok(());
            }
            // Dangling or not, only a link to a Lightbulb app binary is ours to repair.
            if target.file_name() != exe.file_name() {
                return Err("~/.local/bin/lightbulb points at another program; leaving it".into());
            }
        }
        Ok(_) => return Err("~/.local/bin/lightbulb is not Lightbulb's; leaving it".into()),
        Err(_) => {}
    }
    std::fs::create_dir_all(&bin).map_err(|e| e.to_string())?;
    let staged = bin.join(format!(".lightbulb-{}", std::process::id()));
    let _ = std::fs::remove_file(&staged);
    std::os::unix::fs::symlink(exe, &staged).map_err(|e| e.to_string())?;
    std::fs::rename(&staged, &link).map_err(|e| e.to_string())
}

/// The one line that puts `~/.local/bin` on the login shell's PATH, if it is not there.
pub fn path_line(shell: &str, path: &str, home: &Path) -> Option<String> {
    let bin = home.join(".local/bin");
    if std::env::split_paths(path).any(|p| p == bin) {
        return None;
    }
    Some(match Path::new(shell).file_name().and_then(|n| n.to_str()) {
        Some("fish") => "fish_add_path ~/.local/bin".into(),
        Some("bash") => r#"echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bash_profile"#.into(),
        _ => r#"echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc"#.into(),
    })
}

/// At launch. Only an installed app links itself: a copy run from the disk image, a translocated
/// path or a build folder moves away and would leave the link dangling.
pub fn install_at_launch() {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else { return };
    let Ok(exe) = std::env::current_exe().and_then(std::fs::canonicalize) else { return };
    if !(exe.starts_with("/Applications") || exe.starts_with(home.join("Applications"))) {
        return;
    }
    if let Err(e) = install(&exe, &home) {
        crate::log(&format!("[cli] {e}"));
        return;
    }
    // The login shell's PATH, not this GUI app's.
    let env = crate::holder::run::login_environment();
    let path = env.iter().find(|(k, _)| k == "PATH").map(|(_, v)| v.as_str()).unwrap_or_default();
    let _ = INSTALLED.set(path_line(&std::env::var("SHELL").unwrap_or_default(), path, &home));
}

/// For the New session view: null until the command is installed.
pub fn status() -> Value {
    match INSTALLED.get() {
        Some(path_line) => json!({ "pathLine": path_line }),
        None => Value::Null,
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
        for ok in [&[][..], &["resume"], &["resume", "--last"], &["fork"], &["fixit"], &["fix the build"], &["-C", "app"],
            &["--profile", "review"], &["-c", "x=1", "hello"], &["--full-auto", "hello"], &["--", "exec"], &["-m", "o3"]] {
            assert_eq!(refusal("codex", &words(ok)), None, "{ok:?}");
        }
        for no in [&["plugin", "list"][..], &["queue"], &["archive", "x"], &["delete", "x"], &["migrate-rollouts"], &["execpolicy"],
            &["exec", "hi"], &["e", "hi"], &["-m", "o3", "exec", "hi"], &["--model=o3", "review"], &["agents"]] {
            assert!(refusal("codex", &words(no)).is_some(), "{no:?}");
        }
        for no in [&["--version"][..], &["-V"], &["--help"], &["-h"], &["tcp-tunnel"]] {
            assert!(refusal("codex", &words(no)).is_some(), "{no:?}");
        }
        assert_eq!(refusal("codex", &words(&["--", "--version"])), None);
        for no in [&["--version"][..], &["-v"], &["--help"], &["-h"]] {
            assert!(refusal("claude", &words(no)).is_some(), "{no:?}");
        }
        assert_eq!(refusal("claude", &words(&["--", "--help"])), None);
        // The installed Codex's own list catches a command this build has never heard of.
        assert!(codex_refusal(&words(&["newthing"]), &["newthing".to_string()]).is_some());
        assert_eq!(codex_refusal(&words(&["fork"]), &["fork".to_string()]), None);
    }

    #[test]
    fn cli_reads_the_commands_from_codex_help() {
        let help = "Codex CLI\n\nCommands:\n  exec              Run Codex non-interactively [aliases: e]\n  plugin            Manage Codex plugins\n  apply             Apply the latest diff produced by Codex agent as a `git apply` to your local\n                    working tree [aliases: a]\n  fork              Fork a previous interactive session (picker by default; use --last to fork the\n                    most recent)\n\nArguments:\n  [PROMPT]\n";
        let got = help_commands(help);
        for name in ["exec", "e", "plugin", "fork", "apply", "a"] {
            assert!(got.iter().any(|c| c == name), "{name} in {got:?}");
        }
        for word in ["working", "most", "most", "recent)", "Arguments:", "[PROMPT]", "Run"] {
            assert!(!got.iter().any(|c| c == word), "{word} in {got:?}");
        }
        assert!(help_commands("no commands here").is_empty());
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

    #[test]
    fn cli_socket_carries_a_request_whose_json_is_larger_than_its_arguments() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("cli.sock");
        serve_at(&path, |request| {
            if request["start"]["args"][0].as_str().map(str::len) == Some(60_000) {
                Ok(Started { socket: "/h/x.sock".into(), workspace: None })
            } else {
                Err("bad".into())
            }
        })
        .unwrap();
        let quotes = "\"".repeat(60_000);
        assert!(ask_app(&path, &json!({"start": {"args": [quotes]}})).is_ok());
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
        for mode in [&b"\x1b[?2026l"[..], b"\x1b[<u", b"\x1b[?1l", b"\x1b>"] {
            assert!(reset_sequence(false).windows(mode.len()).any(|w| w == mode), "{mode:?}");
        }
    }

    #[test]
    fn cli_names_the_page_workspace_only_when_it_is_the_shared_one() {
        let page = ("org_a".to_string(), "Acme".to_string());
        assert_eq!(shared_workspace(Some(&page), Some("org_a")).as_deref(), Some("Acme"));
        assert_eq!(shared_workspace(Some(&page), Some("org_b")), None, "page switched to another workspace");
        assert_eq!(shared_workspace(Some(&page), None), None);
        assert_eq!(shared_workspace(None, Some("org_a")), None, "signed out");
    }

    #[test]
    fn cli_install_links_repairs_and_never_clobbers() {
        let home = tempfile::tempdir().unwrap();
        let macos = home.path().join("Lightbulb.app/Contents/MacOS");
        std::fs::create_dir_all(&macos).unwrap();
        let exe = macos.join("lightbulb-desktop");
        std::fs::write(&exe, b"").unwrap();
        let link = home.path().join(".local/bin/lightbulb");
        install(&exe, home.path()).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), exe);
        install(&exe, home.path()).unwrap();

        // A link left by a copy of the app that moved away is repaired.
        std::fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(home.path().join("gone/lightbulb-desktop"), &link).unwrap();
        install(&exe, home.path()).unwrap();
        assert_eq!(std::fs::read_link(&link).unwrap(), exe);

        // Someone else's `lightbulb`, a link or a file, stays.
        let other = home.path().join("other-tool");
        std::fs::write(&other, b"").unwrap();
        std::fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(&other, &link).unwrap();
        assert!(install(&exe, home.path()).is_err());
        assert_eq!(std::fs::read_link(&link).unwrap(), other);
        std::fs::remove_file(&link).unwrap();
        std::fs::write(&link, b"#!/bin/sh\n").unwrap();
        assert!(install(&exe, home.path()).is_err());
        assert_eq!(std::fs::read(&link).unwrap(), b"#!/bin/sh\n");

        // An unrelated link whose target is gone is not ours either.
        std::fs::remove_file(&link).unwrap();
        let nowhere = home.path().join("nowhere/other-tool");
        std::os::unix::fs::symlink(&nowhere, &link).unwrap();
        assert!(install(&exe, home.path()).is_err());
        assert_eq!(std::fs::read_link(&link).unwrap(), nowhere);
    }

    #[test]
    fn cli_path_line_only_when_the_login_path_lacks_the_folder() {
        let home = Path::new("/Users/a");
        assert_eq!(path_line("/bin/zsh", "/usr/bin:/Users/a/.local/bin", home), None);
        assert_eq!(path_line("/bin/zsh", "/usr/bin:/Users/a/.local/bin/", home), None);
        assert_eq!(path_line("/bin/zsh", "/usr/bin", home).unwrap(), r#"echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc"#);
        assert_eq!(path_line("/bin/bash", "/usr/bin", home).unwrap(), r#"echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bash_profile"#);
        assert_eq!(path_line("/opt/homebrew/bin/fish", "/usr/bin", home).unwrap(), "fish_add_path ~/.local/bin");
    }
}
