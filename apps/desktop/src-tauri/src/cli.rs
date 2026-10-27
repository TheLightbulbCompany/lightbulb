//! The `lightbulb` command (spec §5.3): `lightbulb claude|codex [any provider arguments]` asks
//! the running app to start a held, shared session in this folder, says who it is shared with,
//! then is that session's terminal.
//!
//! The app does the start (`session_start::cli_start`): the installed tool, and sharing must be
//! on. Any folder: starting a session with Lightbulb is what shares it. The provider's arguments
//! pass through unchanged (owner ruling 2026-10-03). It never blocks a person (owner ruling
//! 2026-10-04): a form with no terminal to share, or a start the app cannot share, runs the real
//! tool instead. This process only turns words into that request and moves bytes.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::CommandExt;
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
        // --bg starts a background job and returns: the job is shared through `claude attach` (spec §4 row c), so the real tool runs.
        let refused = tool == "claude"
            && (arg == "--print"
                || arg == "--bg"
                || arg == "--background"
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
/// The app may have started a session: never start the tool a second time after this one.
const NO_ANSWER: &str = "Lightbulb did not answer. If a session started, it is in the Lightbulb app.";
/// The calling terminal's environment, raw bytes: well over any real one, far under the line limit.
const MAX_ENV: usize = 256 << 10;

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
    lightbulb_core::os::peer(conn).is_ok_and(|(uid, _)| uid == lightbulb_core::os::euid())
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
    // JSON escapes a byte to at most 6 (`\u00XX`), so 64 KiB of arguments and 256 KiB of
    // environment serialize under 1920 KiB plus the envelope.
    BufReader::new((&conn).take(2 << 20)).read_line(&mut line)?;
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
        let env = environment(&request["env"])?;
        let info = crate::session_start::cli_start(&app, request["start"].clone(), size, env)?;
        let owner = crate::local_sessions::enrollment::state_dir(&app).ok().and_then(|d| crate::local_sessions::enrollment::enrolled_owner(&d));
        let workspace = shared_workspace(WORKSPACE.lock().unwrap().as_ref(), owner.as_deref());
        Ok(Started { socket: info.socket, workspace })
    })
    .map_err(|e| e.to_string())
}

/// The request's terminal environment, if it has one: string values, no NUL, within `MAX_ENV`.
pub fn environment(value: &Value) -> Result<Option<Vec<(String, String)>>, String> {
    let Some(map) = value.as_object() else { return Ok(None) };
    let mut total = 0;
    let mut out = vec![];
    for (k, v) in map {
        let v = v.as_str().ok_or("Malformed request.")?;
        total += k.len() + v.len();
        if k.is_empty() || k.contains(['=', '\0']) || v.contains('\0') || total > MAX_ENV {
            return Err("Malformed request.".into());
        }
        out.push((k.clone(), v.to_string()));
    }
    Ok(Some(out))
}

/// This terminal's environment for the session, minus what only describes this shell.
pub fn terminal_environment() -> serde_json::Map<String, Value> {
    std::env::vars_os()
        .map(|(k, v)| (k.to_string_lossy().into_owned(), v.to_string_lossy().into_owned()))
        .filter(|(k, _)| !matches!(k.as_str(), "PWD" | "OLDPWD" | "SHLVL" | "_"))
        .map(|(k, v)| (k, Value::String(v)))
        .collect()
}

/// The command's end: ask the running app to start the session.
pub fn ask_app(path: &Path, request: &Value) -> Result<Started, String> {
    fn no_answer<E>(_: E) -> String {
        NO_ANSWER.into()
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
use crate::holder::protocol::{encode, read_frame, split_offset, DETACHED, END, EXIT, HELLO, INPUT, OUTPUT, RESIZE, SNAPSHOT};
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
       lightbulb continue '<link>'\n\
       lightbulb share on|off | pair\n\
Starts a session in the Lightbulb app on this Mac, with its live terminal there too, or continues\n\
a shared session from its link in your own Claude Code.";

/// `lightbulb continue '<link>'`: Some when the first word is `continue`.
pub fn continue_link(args: &[String]) -> Option<Result<&str, ()>> {
    match args {
        [first, link] if first == "continue" => Some(Ok(link.as_str())),
        [first, ..] if first == "continue" => Some(Err(())),
        _ => None,
    }
}

pub fn parse(args: &[String]) -> Result<Ask, String> {
    let Some((tool, rest)) = args.split_first().filter(|(t, _)| *t == "claude" || *t == "codex") else {
        return Err(USAGE.into());
    };
    if let Some(reason) = refusal(tool, rest) {
        return Err(reason);
    }
    Ok(Ask { tool: tool.clone(), args: rest.to_vec() })
}

/// Exactly `session_start::cli_start`'s fields, plus this terminal's size and environment.
pub fn request(ask: &Ask, folder: &Path, (cols, rows): (u16, u16), env: serde_json::Map<String, Value>) -> Value {
    json!({
        "start": {"tool": ask.tool, "folder": folder.to_string_lossy(), "args": ask.args,
            "startId": uuid::Uuid::new_v4().to_string()},
        "cols": cols, "rows": rows, "env": env,
    })
}

/// The app's refusal as the short reason in "no live terminal (…)": its first sentence.
pub fn short_reason(message: &str) -> String {
    message.split(". ").next().unwrap_or(message).trim_end_matches('.').to_string()
}

/// Run the real tool in place of this process, with the same arguments, by absolute path (so
/// never this command again). `reason` is why a session that could have been shared is not.
fn run_real(tool: &str, args: &[String], reason: Option<&str>) -> i32 {
    unsafe {
        let mut none: libc::sigset_t = std::mem::zeroed();
        libc::sigemptyset(&mut none);
        libc::pthread_sigmask(libc::SIG_SETMASK, &none, std::ptr::null_mut());
    }
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
    let own = std::env::current_exe().and_then(std::fs::canonicalize).ok();
    // The one this shell would have run: its PATH order first (asdf/mise shims, a pinned version),
    // then the app's known install places. Never this command again.
    let Some(program) = on_path(tool, std::env::var_os("PATH").as_deref(), own.as_deref())
        .or_else(|| crate::local_sessions::provider_program(&home, tool).filter(|p| Some(p) != own.as_ref()))
    else {
        eprintln!("lightbulb: {tool} is not installed.");
        return 127;
    };
    if let Some(reason) = reason {
        eprintln!("lightbulb: no live terminal ({reason}), running {tool} normally");
    }
    let err = std::process::Command::new(&program).arg0(tool).args(args).exec();
    eprintln!("lightbulb: could not run {}: {err}", program.display());
    126
}

/// The first executable `tool` on `path`, as the shell resolves a command, skipping `own` (this
/// program, should a link named `tool` point back at it).
fn on_path(tool: &str, path: Option<&std::ffi::OsStr>, own: Option<&Path>) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    std::env::split_paths(path?).map(|dir| dir.join(tool)).find(|candidate| {
        let real = std::fs::canonicalize(candidate).ok();
        candidate.is_file()
            && std::fs::metadata(candidate).is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
            && real.as_deref() != own
    })
}

/// The workspace name comes from the web page: never let it write escape sequences here.
pub fn shared_line(workspace: Option<&str>) -> String {
    let name: String = workspace.unwrap_or("").chars().filter(|c| !c.is_control()).take(80).collect();
    let name = match name.trim() {
        "" => "your Lightbulb workspace",
        name => name,
    };
    format!("● Shared with {name}.")
}

#[derive(Debug, PartialEq)]
pub enum Outcome {
    /// The provider exited with this code.
    Exited(i32),
    /// Another interactive client took the keyboard (the app's panel joins instead, so only a
    /// second terminal does); the session keeps running.
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

/// SIGWINCH resizes the session. SIGHUP (the terminal closed) ends it, as closing the terminal
/// ends the provider run directly (`signal_frame`). SIGINT and SIGTERM detach and leave it
/// running. Blocked before any thread starts, so only `sigwait` receives them.
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

/// The frame a signal sends the holder: SIGWINCH the window's new size, SIGHUP END. Closing the
/// terminal ends the session (it never runs on headless with nothing to say so); a holder of a
/// session the provider's own daemon hosts ends only its client, and the job runs on in the
/// background, as Claude's own terminal does.
fn signal_frame(sig: libc::c_int, (cols, rows): (u16, u16)) -> Option<Vec<u8>> {
    match sig {
        libc::SIGWINCH => {
            let mut size = cols.to_be_bytes().to_vec();
            size.extend_from_slice(&rows.to_be_bytes());
            Some(encode(RESIZE, &size))
        }
        libc::SIGHUP => Some(encode(END, b"")),
        _ => None,
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
        if let Some(frame) = signal_frame(sig, if sig == libc::SIGWINCH { window_size() } else { (0, 0) }) {
            let _ = resize.lock().unwrap().write_all(&frame);
        }
        if sig != libc::SIGWINCH {
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
        Outcome::Moved => (0, Some("● This session was opened in another terminal. It keeps running in the Lightbulb app.")),
        Outcome::Lost => match STOPPED_BY.load(Ordering::SeqCst) {
            0 => (1, Some("● Lost this session's terminal. If it is still running, it is in the Lightbulb app.")),
            libc::SIGHUP => (128 + libc::SIGHUP, None),
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
    match args.iter().map(String::as_str).collect::<Vec<_>>()[..] {
        // `join` is the old name, kept for scripts; the engine owns `lightbulb join` now.
        ["pair"] | ["join"] => return crate::headless::join(),
        ["setup"] => return crate::headless::setup(),
        ["leave"] => return crate::headless::leave(),
        ["share", "on"] => return crate::headless::share(true),
        ["share", "off"] => return crate::headless::share(false),
        ["share", ..] => {
            eprintln!("Usage: lightbulb share on|off");
            return 2;
        }
        _ => {}
    }
    // Before `block_signals`, so Ctrl-C works while it waits for the browser.
    if let Some(link) = continue_link(args) {
        return match link {
            Ok(link) => crate::session_continue::main(link),
            Err(()) => {
                eprintln!("Usage: lightbulb continue '<link>'");
                2
            }
        };
    }
    // First, so a Ctrl-C or SIGHUP while the app starts the session stays pending and detaches
    // with the Decision 4 line in `attach`, instead of killing this silently with the session
    // started anyway. The early exits below just exit with it pending.
    let signals = block_signals();
    // A form with no terminal to share (`-p`, `--version`, `codex exec`, …) runs the real tool,
    // with no note: it was never going to be shared.
    let ask = match parse(args) {
        Ok(ask) => ask,
        Err(message) => match args.first().filter(|t| *t == "claude" || *t == "codex") {
            Some(tool) => return run_real(tool, &args[1..], None),
            None => {
                eprintln!("{message}");
                return 2;
            }
        },
    };
    // Never start a session nobody here can see; scripts and pipes get the real tool.
    if unsafe { libc::isatty(0) != 1 || libc::isatty(1) != 1 } {
        return run_real(&ask.tool, &ask.args, None);
    }
    let Ok(folder) = std::env::current_dir() else {
        return run_real(&ask.tool, &ask.args, Some("this folder no longer exists"));
    };
    let size = window_size();
    let socket = app_socket();
    let reply = ask_app(&socket, &request(&ask, &folder, size, terminal_environment()));
    match reply {
        Ok(started) => {
            println!("{}", shared_line(started.workspace.as_deref()));
            attach(&started.socket, size, signals)
        }
        Err(message) if message == NO_ANSWER => {
            eprintln!("{message}");
            1
        }
        Err(message) => run_real(&ask.tool, &ask.args, Some(&short_reason(&message))),
    }
}

/// Set once `~/.local/bin/lightbulb` is in place: the PATH line to show, if one is needed.
static INSTALLED: OnceLock<Option<String>> = OnceLock::new();

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

/// `exe` without the `/.nofollow` prefix macOS can report on a running app.
pub(crate) fn strip_nofollow(exe: &Path) -> PathBuf {
    exe.strip_prefix("/.nofollow").map(|p| Path::new("/").join(p)).unwrap_or_else(|_| exe.to_path_buf())
}

/// The path to link to, if `exe` is an installed app. macOS can report the running app as
/// `/.nofollow/Applications/...` (and `canonicalize` keeps that), so the prefix is dropped first.
pub(crate) fn installed_exe(exe: &Path, home: &Path) -> Option<PathBuf> {
    let exe = strip_nofollow(exe);
    (exe.starts_with("/Applications") || exe.starts_with(home.join("Applications"))).then_some(exe)
}

/// At launch: the PATH line, once the `lightbulb` command is in ~/.local/bin. The network
/// engine owns that link (its `setup` points it at the engine, which hands `claude`, `codex`,
/// `continue` and `share` to `lightbulb-desktop cli`); the app no longer writes it.
pub fn install_at_launch() {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else { return };
    let Ok(exe) = std::env::current_exe().and_then(std::fs::canonicalize) else { return };
    // Not an installed app (a build folder, the disk image): nothing to show, nothing to log.
    if installed_exe(&exe, &home).is_none() {
        return;
    }
    match link_command(&exe, &home) {
        Ok(line) => {
            let _ = INSTALLED.set(line);
        }
        Err(e) => crate::log(&format!("[cli] {e}")),
    }
}

/// For an installed app whose `~/.local/bin/lightbulb` exists (the engine's link; never
/// written here): the PATH line to show, if one is needed.
pub fn link_command(exe: &Path, home: &Path) -> Result<Option<String>, String> {
    installed_exe(exe, home).ok_or("the lightbulb command is set up only for an app in /Applications")?;
    if !home.join(".local/bin/lightbulb").exists() {
        return Err("~/.local/bin/lightbulb is not installed yet; the network engine installs it".into());
    }
    // The login shell's PATH, not this GUI app's.
    let env = crate::holder::run::login_environment();
    let path = env.iter().find(|(k, _)| k == "PATH").map(|(_, v)| v.as_str()).unwrap_or_default();
    Ok(path_line(&std::env::var("SHELL").unwrap_or_default(), path, home))
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
    fn continue_takes_exactly_one_link() {
        assert_eq!(
            continue_link(&words(&["continue", "https://x/s#t"])),
            Some(Ok("https://x/s#t"))
        );
        assert_eq!(continue_link(&words(&["continue"])), Some(Err(())));
        assert_eq!(
            continue_link(&words(&["continue", "a", "b"])),
            Some(Err(()))
        );
        assert_eq!(continue_link(&words(&["claude", "continue"])), None);
    }

    #[test]
    fn cli_refuses_only_the_forms_with_no_terminal_to_share() {
        for ok in [&["--resume", "abc"][..], &["--dangerously-skip-permissions"], &["--continue"],
            &["--permission-mode", "bypassPermissions"], &["--model", "opus", "fix it"], &["--", "-p"], &[]] {
            assert_eq!(refusal("claude", &words(ok)), None, "{ok:?}");
        }
        for no in [&["-p", "hi"][..], &["--print", "hi"], &["-cp", "hi"], &["--print=json"], &["--bg"], &["--bg", "fix it"],
            &["--background", "fix it"], &["--resume", "abc", "--bg"]] {
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
        assert_eq!(refusal("claude", &words(&["--", "--bg"])), None, "a prompt that says --bg is the prompt");
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
    fn cli_fallback_follows_the_shells_path_and_skips_itself() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let (first, second) = (dir.path().join("a"), dir.path().join("b"));
        std::fs::create_dir_all(&first).unwrap();
        std::fs::create_dir_all(&second).unwrap();
        for d in [&first, &second] {
            let f = d.join("claude");
            std::fs::write(&f, "#!/bin/sh\n").unwrap();
            std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let path = std::env::join_paths([&first, &second]).unwrap();
        assert_eq!(on_path("claude", Some(&path), None), Some(first.join("claude")), "PATH order");
        let own = std::fs::canonicalize(first.join("claude")).unwrap();
        assert_eq!(on_path("claude", Some(&path), Some(&own)), Some(second.join("claude")), "never itself");
        assert_eq!(on_path("codex", Some(&path), None), None);
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
        let mut env = serde_json::Map::new();
        env.insert("FOO".into(), json!("bar"));
        let request = request(&ask, Path::new("/Users/a/code/app"), (80, 24), env);
        assert_eq!(request["env"]["FOO"], "bar", "the terminal's environment, beside start");
        assert_eq!(request["start"]["tool"], "claude");
        assert_eq!(request["start"]["folder"], "/Users/a/code/app");
        assert_eq!(request["start"]["args"], json!(["--resume", "abc", "--dangerously-skip-permissions", "fix it"]));
        assert_eq!((request["cols"].as_u64(), request["rows"].as_u64()), (Some(80), Some(24)));
        let keys: Vec<_> = request["start"].as_object().unwrap().keys().cloned().collect();
        assert_eq!(keys, ["tool", "folder", "args", "startId"], "exactly cli_start's fields");
    }

    #[test]
    fn cli_environment_is_strings_only_and_bounded() {
        assert_eq!(environment(&Value::Null), Ok(None), "app-started: the login environment");
        assert_eq!(environment(&json!({"A": "1"})), Ok(Some(vec![("A".into(), "1".into())])));
        for bad in [json!({"A": 1}), json!({"": "x"}), json!({"A=B": "x"}), json!({"A": "x\0"}), json!({"A": "x".repeat(MAX_ENV)})] {
            assert!(environment(&bad).is_err(), "{bad}");
        }
        assert!(!terminal_environment().contains_key("PWD"));
    }

    #[test]
    fn cli_short_reason_is_the_app_s_first_sentence() {
        assert_eq!(short_reason(NOT_RUNNING), "Lightbulb is not running");
        assert_eq!(short_reason("claude is not installed on this Mac."), "claude is not installed on this Mac");
    }

    #[test]
    fn cli_shared_line_names_the_workspace_and_never_prints_its_control_bytes() {
        assert_eq!(shared_line(Some("Acme")), "● Shared with Acme.");
        assert_eq!(shared_line(Some("Ev\x1b]0;pwned\x07il")), "● Shared with Ev]0;pwnedil.");
        assert_eq!(shared_line(None), "● Shared with your Lightbulb workspace.");
    }

    #[test]
    fn cli_closing_the_terminal_ends_the_session_and_a_resize_sends_the_size() {
        assert_eq!(signal_frame(libc::SIGHUP, (0, 0)), Some(encode(crate::holder::protocol::END, b"")));
        assert_eq!(signal_frame(libc::SIGWINCH, (120, 40)), Some(encode(crate::holder::protocol::RESIZE, &[0, 120, 0, 40])));
        assert_eq!(signal_frame(libc::SIGINT, (0, 0)), None, "an interrupt detaches and leaves it running");
        assert_eq!(signal_frame(libc::SIGTERM, (0, 0)), None);
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
    fn cli_never_writes_the_lightbulb_link() {
        let home = tempfile::tempdir().unwrap();
        let exe = home.path().join("Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        assert!(link_command(&exe, home.path()).is_err(), "no command yet: nothing to show");
        assert!(std::fs::symlink_metadata(home.path().join(".local/bin/lightbulb")).is_err(), "and none is made");
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

    #[test]
    fn cli_installs_from_applications_with_or_without_nofollow() {
        let home = Path::new("/Users/a");
        let plain = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        let nofollow = Path::new("/.nofollow/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        assert_eq!(installed_exe(nofollow, home).as_deref(), Some(plain));
        assert_eq!(installed_exe(plain, home).as_deref(), Some(plain));
        let user = Path::new("/Users/a/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        assert_eq!(installed_exe(user, home).as_deref(), Some(user));
        for refused in ["/Users/a/src/lightbulb/target/release/lightbulb-desktop",
            "/Volumes/Lightbulb/Lightbulb.app/Contents/MacOS/lightbulb-desktop",
            "/.nofollow/Volumes/Lightbulb/Lightbulb.app/Contents/MacOS/lightbulb-desktop"] {
            assert_eq!(installed_exe(Path::new(refused), home), None, "{refused}");
        }
    }
}
