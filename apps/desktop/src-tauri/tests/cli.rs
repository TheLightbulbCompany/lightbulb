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
        let pair = native_pty_system().openpty(PtySize { rows: 24, cols: 80, pixel_width: 0, pixel_height: 0 }).unwrap();
        let mut cmd = CommandBuilder::new(env!("CARGO_BIN_EXE_lightbulb-desktop"));
        cmd.args(["cli", "claude"]);
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
fn cli_says_the_app_is_not_running_and_exits_1() {
    let mut cli = Cli::start(&temp_dir().join("no-app.sock"));
    assert_eq!(cli.exit_code(), 1);
    cli.wait_for("Lightbulb is not running");
}

#[test]
fn cli_prints_the_app_s_refusal_and_attaches_to_nothing() {
    let app = temp_dir().join("app.sock");
    serve_at(&app, |_| Err("This folder is not inside a folder you approved for Lightbulb. Add it in the Lightbulb app under Settings → File access, then run this again.".into())).unwrap();
    let mut cli = Cli::start(&app);
    assert_eq!(cli.exit_code(), 1);
    cli.wait_for("not inside a folder you approved");
    assert!(!cli.text().contains("Shared with"));
}

#[test]
fn cli_restores_the_terminal_after_the_child_exits_and_exits_with_its_code() {
    let (app, _) = app_with_holder(&["/bin/sh", "-c", "read x; printf done; exit 3"]);
    let mut cli = Cli::start(&app);
    cli.wait_for("● Shared with Acme. Run plain `claude` to keep a session private.");
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
    eventually("the session to end", || !holder.exists());
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
