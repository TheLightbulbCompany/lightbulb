//! The `lightbulb` command end to end: the real binary in a pty, a fake app on a test socket,
//! and a holder running `/bin/sh` or `/bin/cat` in place of a provider.
#![cfg(unix)]
use lightbulb_desktop::cli::{serve_at, Started};
use lightbulb_desktop::holder::{manager, protocol::*, run};
use portable_pty::{native_pty_system, Child, CommandBuilder, MasterPty, PtySize, SlavePty};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

fn temp_dir() -> PathBuf {
    let dir = PathBuf::from(format!("/private/tmp/lbc-test-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn eventually(what: &str, mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !done() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// A holder running `argv`, and a fake app that hands its socket to the command.
fn app_with_holder(argv: &[&str]) -> (PathBuf, PathBuf) {
    let dir = temp_dir();
    let holder = dir.join("held.sock");
    let (s, d) = (holder.clone(), dir.clone());
    let argv: Vec<String> = argv.iter().map(|a| a.to_string()).collect();
    std::thread::spawn(move || run::run_with_env(&s, &argv, &d, vec![("PATH".into(), "/usr/bin:/bin".into())], Some((80, 24))));
    eventually("the holder", || manager::hello(&holder).is_some());
    let app = dir.join("app.sock");
    let socket = holder.clone();
    serve_at(&app, move |_| Ok(Started { socket: socket.clone(), workspace: Some("Acme".into()) })).unwrap();
    (app, holder)
}

struct Cli {
    child: Box<dyn Child + Send + Sync>,
    master: Box<dyn MasterPty + Send>,
    /// Kept open so the terminal's settings can still be read after the command exits.
    _slave: Box<dyn SlavePty + Send>,
    keys: Box<dyn Write + Send>,
    seen: Arc<Mutex<Vec<u8>>>,
}

impl Cli {
    fn start(app: &Path) -> Cli {
        Cli::start_args(app, &["claude"])
    }

    /// A HOME whose `claude` is a stand-in for the real one, found before any installed copy.
    fn start_args(app: &Path, args: &[&str]) -> Cli {
        let home = temp_dir();
        std::fs::create_dir_all(home.join(".local/bin")).unwrap();
        let real = home.join(".local/bin/claude");
        std::fs::write(&real, "#!/bin/sh\necho \"real claude $*\"\nexit 7\n").unwrap();
        std::fs::set_permissions(&real, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        let pair = native_pty_system().openpty(PtySize { rows: 24, cols: 80, pixel_width: 0, pixel_height: 0 }).unwrap();
        let mut cmd = CommandBuilder::new(env!("CARGO_BIN_EXE_lightbulb-desktop"));
        cmd.arg("cli");
        cmd.args(args);
        cmd.env("HOME", &home);
        // The stand-in must also be the first `claude` on PATH: the command runs the one this shell
        // would have run, and a machine with Claude Code installed would start the real one.
        cmd.env("PATH", format!("{}:/usr/bin:/bin", home.join(".local/bin").display()));
        cmd.env("LIGHTBULB_TEST_MARKER", "from-the-terminal");
        cmd.env("LIGHTBULB_TEST_APP_SOCKET", app);
        cmd.cwd("/private/tmp");
        let child = pair.slave.spawn_command(cmd).unwrap();
        let mut reader = pair.master.try_clone_reader().unwrap();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let sink = seen.clone();
        std::thread::spawn(move || {
            let mut buf = [0u8; 4096];
            while let Ok(n) = reader.read(&mut buf) {
                if n == 0 {
                    break;
                }
                sink.lock().unwrap().extend_from_slice(&buf[..n]);
            }
        });
        let keys = pair.master.take_writer().unwrap();
        Cli { child, master: pair.master, _slave: pair.slave, keys, seen }
    }

    fn text(&self) -> String {
        String::from_utf8_lossy(&self.seen.lock().unwrap()).into_owned()
    }

    fn wait_for(&self, text: &str) {
        eventually(text, || self.text().contains(text));
    }

    /// Line editing, echo, and Ctrl-C as a signal: what a shell expects back.
    fn cooked(&self) -> bool {
        let mut t: libc::termios = unsafe { std::mem::zeroed() };
        assert_eq!(unsafe { libc::tcgetattr(self.master.as_raw_fd().unwrap(), &mut t) }, 0);
        let on = libc::ICANON | libc::ECHO | libc::ISIG;
        t.c_lflag & on == on
    }

    fn wait_raw(&self) {
        eventually("raw mode", || !self.cooked());
    }

    fn exit_code(&mut self) -> u32 {
        let mut code = None;
        eventually("the command to exit", || {
            code = self.child.try_wait().unwrap().map(|s| s.exit_code());
            code.is_some()
        });
        code.unwrap()
    }
}

#[test]
fn cli_runs_the_real_tool_when_the_app_is_not_running() {
    let mut cli = Cli::start_args(&temp_dir().join("no-app.sock"), &["claude", "--model", "opus"]);
    assert_eq!(cli.exit_code(), 7, "the real tool's own exit code");
    cli.wait_for("lightbulb: no live terminal (Lightbulb is not running), running claude normally");
    cli.wait_for("real claude --model opus");
}

#[test]
fn cli_runs_the_real_tool_after_the_app_s_refusal() {
    let app = temp_dir().join("app.sock");
    serve_at(&app, |_| Err("Session sharing is off in Lightbulb on this Mac. Turn it on in Settings.".into())).unwrap();
    let mut cli = Cli::start(&app);
    assert_eq!(cli.exit_code(), 7);
    cli.wait_for("no live terminal (Session sharing is off in Lightbulb on this Mac)");
    cli.wait_for("real claude");
    assert!(!cli.text().contains("Shared with"));
}

#[test]
fn cli_runs_non_interactive_forms_as_the_real_tool_without_a_note() {
    let mut cli = Cli::start_args(&temp_dir().join("no-app.sock"), &["claude", "-p", "hi"]);
    assert_eq!(cli.exit_code(), 7);
    cli.wait_for("real claude -p hi");
    assert!(!cli.text().contains("not shared"));
}

#[test]
fn cli_starts_in_any_folder_without_a_question_and_with_the_terminal_s_environment() {
    let (_, holder) = app_with_holder(&["/bin/cat"]);
    let seen = Arc::new(Mutex::new(Vec::<serde_json::Value>::new()));
    let app = temp_dir().join("app.sock");
    let requests = seen.clone();
    serve_at(&app, move |request| {
        requests.lock().unwrap().push(request.clone());
        Ok(Started { socket: holder.clone(), workspace: Some("Acme".into()) })
    })
    .unwrap();
    let cli = Cli::start(&app);
    cli.wait_for("● Shared with Acme.");
    let requests = seen.lock().unwrap().clone();
    assert_eq!(requests.len(), 1, "one request: starting with Lightbulb is what shares it");
    assert!(requests[0].get("approveFolder").is_none() && !cli.text().contains("[y/N]"));
    assert_eq!(requests[0]["env"]["LIGHTBULB_TEST_MARKER"], "from-the-terminal", "the terminal's environment");
    assert!(requests[0]["env"].get("PWD").is_none());
}

#[test]
fn cli_restores_the_terminal_after_the_child_exits_and_exits_with_its_code() {
    let (app, _) = app_with_holder(&["/bin/sh", "-c", "read x; printf done; exit 3"]);
    let mut cli = Cli::start(&app);
    cli.wait_for("● Shared with Acme.");
    cli.wait_raw();
    cli.keys.write_all(b"go\r").unwrap();
    assert_eq!(cli.exit_code(), 3);
    cli.wait_for("done");
    assert!(cli.cooked());
}

#[test]
fn cli_restores_the_terminal_after_ctrl_c_ends_the_session() {
    let (app, holder) = app_with_holder(&["/bin/cat"]);
    let mut cli = Cli::start(&app);
    cli.wait_raw();
    // Raw mode: Ctrl-C is a byte for the session's own terminal, which interrupts the provider.
    cli.keys.write_all(b"\x03").unwrap();
    assert_eq!(cli.exit_code(), 1, "a provider killed by a signal reports 1");
    assert!(cli.cooked());
    eventually("the session to end", || manager::hello(&holder).is_some_and(|h| manager::is_ended(&h)));
}

#[test]
fn cli_interrupted_restores_the_terminal_and_leaves_the_session_running() {
    let (app, holder) = app_with_holder(&["/bin/cat"]);
    let mut cli = Cli::start(&app);
    cli.wait_raw();
    unsafe { libc::kill(cli.child.process_id().unwrap() as libc::pid_t, libc::SIGINT) };
    assert_eq!(cli.exit_code(), 130);
    assert!(cli.cooked());
    cli.wait_for("keeps running in the Lightbulb app");
    assert!(manager::hello(&holder).is_some(), "the session outlives its outside terminal");
}

#[test]
fn cli_attach_detaches_the_other_interactive_client() {
    let (app, holder) = app_with_holder(&["/bin/cat"]);
    let mut panel = UnixStream::connect(&holder).unwrap();
    panel.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    panel.write_all(&encode(HELLO, br#"{"role":"interactive"}"#)).unwrap();
    let cli = Cli::start(&app);
    while read_frame(&mut panel).expect("DETACHED before the timeout").0 != DETACHED {}
    cli.wait_raw();
    let mut cli = cli;
    cli.keys.write_all(b"typed-in-cli\r").unwrap();
    cli.wait_for("typed-in-cli");
}

#[test]
fn cli_keeps_the_session_when_the_app_panel_joins_it() {
    let (app, holder) = app_with_holder(&["/bin/cat"]);
    let mut cli = Cli::start(&app);
    cli.wait_raw();
    let mut panel = UnixStream::connect(&holder).unwrap();
    panel.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    panel.write_all(&encode(HELLO, br#"{"role":"interactive","join":true}"#)).unwrap();
    let (_, hello) = read_frame(&mut panel).unwrap();
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&hello).unwrap()["joined"], true);
    panel.write_all(&encode(INPUT, b"typed-in-panel\r")).unwrap();
    cli.wait_for("typed-in-panel");
    cli.keys.write_all(b"typed-in-cli\r").unwrap();
    cli.wait_for("typed-in-cli");
    assert!(cli.child.try_wait().unwrap().is_none(), "the terminal stays attached");
}

#[test]
fn cli_leaves_with_code_0_when_another_terminal_takes_the_session() {
    let (app, holder) = app_with_holder(&["/bin/cat"]);
    let mut cli = Cli::start(&app);
    cli.wait_raw();
    let mut other = UnixStream::connect(&holder).unwrap();
    other.write_all(&encode(HELLO, br#"{"role":"interactive"}"#)).unwrap();
    assert_eq!(cli.exit_code(), 0);
    assert!(cli.cooked());
    cli.wait_for("opened in another terminal");
    assert!(manager::hello(&holder).is_some(), "the session keeps running in the app");
}
