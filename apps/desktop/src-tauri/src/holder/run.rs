//! The holder process: one pty, one provider, a terminal model, an offset-addressed output log,
//! and a unix socket. One interactive client at a time; any number of output-only clients.

use super::model::{is_probe_reply, Model};
use super::protocol::*;
use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use std::collections::{HashMap, VecDeque};
use std::io::{Result, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::mpsc::channel;

/// Output kept for `REPLAY`. Older output is answered with `GAP`.
pub const LOG_CAP: usize = 1 << 20;
const ROWS: u16 = 40;
const COLS: u16 = 120;

/// The user's login-shell environment, read once (as VS Code does). The provider is started
/// directly with it: running it *through* an interactive shell runs the user's rc prompts
/// (oh-my-zsh "update? [Y/n]") inside the session.
pub fn login_environment() -> Vec<(String, String)> {
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    let out = std::process::Command::new(&shell)
        .args(["-l", "-i", "-c", "env -0"])
        .env("DISABLE_AUTO_UPDATE", "true")
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .map(|o| o.stdout)
        .unwrap_or_default();
    out.split(|b| *b == 0)
        .filter_map(|pair| {
            let pair = String::from_utf8_lossy(pair);
            let (k, v) = pair.split_once('=')?;
            (!k.is_empty() && !matches!(k, "SHLVL" | "_" | "PWD" | "OLDPWD")).then(|| (k.to_owned(), v.to_owned()))
        })
        .collect()
}

enum Event {
    Output(Vec<u8>),
    Connect(u64, UnixStream),
    Frame(u64, u8, Vec<u8>),
    Gone(u64),
    Exit(String),
}

pub fn run(socket: &Path, argv: &[String], cwd: &Path) -> Result<()> {
    run_with_env(socket, argv, cwd, login_environment())
}

pub fn run_with_env(socket: &Path, argv: &[String], cwd: &Path, env: Vec<(String, String)>) -> Result<()> {
    let program = argv.first().ok_or_else(|| std::io::Error::other("no program"))?;
    let pair = native_pty_system()
        .openpty(PtySize { rows: ROWS, cols: COLS, pixel_width: 0, pixel_height: 0 })
        .map_err(std::io::Error::other)?;
    let mut cmd = CommandBuilder::new(program);
    cmd.args(&argv[1..]);
    cmd.env_clear();
    for (k, v) in env {
        cmd.env(k, v);
    }
    // A GUI app's environment has none of these.
    cmd.env("TERM", "xterm-256color");
    cmd.env("COLORTERM", "truecolor");
    if cmd.get_env("LANG").is_none() {
        cmd.env("LANG", "en_US.UTF-8");
    }
    cmd.cwd(cwd);
    let mut child = pair.slave.spawn_command(cmd).map_err(std::io::Error::other)?;
    drop(pair.slave);
    let provider_pid = child.process_id().unwrap_or(0);
    let mut reader = pair.master.try_clone_reader().map_err(std::io::Error::other)?;
    let mut pty = pair.master.take_writer().map_err(std::io::Error::other)?;

    let _ = std::fs::remove_file(socket);
    let listener = UnixListener::bind(socket)?;
    std::fs::set_permissions(socket, std::fs::Permissions::from_mode(0o600))?;

    let (tx, rx) = channel::<Event>();
    let out_tx = tx.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 65536];
        while let Ok(n) = std::io::Read::read(&mut reader, &mut buf) {
            if n == 0 || out_tx.send(Event::Output(buf[..n].to_vec())).is_err() {
                break;
            }
        }
    });
    let exit_tx = tx.clone();
    std::thread::spawn(move || {
        let status = child.wait().map(|s| format!("{s:?}")).unwrap_or_else(|e| e.to_string());
        let _ = exit_tx.send(Event::Exit(status));
    });
    std::thread::spawn(move || {
        for (id, conn) in (1u64..).zip(listener.incoming()) {
            let Ok(conn) = conn else { continue };
            let Ok(mut read_side) = conn.try_clone() else { continue };
            if tx.send(Event::Connect(id, conn)).is_err() {
                return;
            }
            let tx = tx.clone();
            std::thread::spawn(move || loop {
                match read_frame(&mut read_side) {
                    Ok((kind, payload)) => {
                        if tx.send(Event::Frame(id, kind, payload)).is_err() {
                            return;
                        }
                    }
                    Err(_) => {
                        let _ = tx.send(Event::Gone(id));
                        return;
                    }
                }
            });
        }
    });

    let mut model = Model::new(ROWS, COLS);
    let mut log: VecDeque<u8> = VecDeque::new();
    let mut total: u64 = 0;
    let mut clients: HashMap<u64, UnixStream> = HashMap::new();
    let mut interactive: Option<u64> = None;
    let send = |clients: &mut HashMap<u64, UnixStream>, id: u64, frame: &[u8]| {
        if clients.get_mut(&id).is_some_and(|c| c.write_all(frame).is_err()) {
            clients.remove(&id);
        }
    };

    for event in rx {
        match event {
            Event::Output(bytes) => {
                let answers = model.feed(&bytes);
                if !answers.is_empty() {
                    let _ = pty.write_all(&answers);
                }
                let frame = encode(OUTPUT, &with_offset(total, &bytes));
                total += bytes.len() as u64;
                log.extend(&bytes);
                let excess = log.len().saturating_sub(LOG_CAP);
                log.drain(..excess);
                clients.retain(|_, c| c.write_all(&frame).is_ok());
            }
            Event::Connect(id, conn) => {
                clients.insert(id, conn);
            }
            Event::Frame(id, HELLO, payload) => {
                let role = serde_json::from_slice::<serde_json::Value>(&payload).unwrap_or_default();
                let (rows, cols) = model.size();
                let facts = serde_json::json!({"protocol": 1, "pid": std::process::id(),
                    "providerPid": provider_pid, "argv": argv, "cwd": cwd, "rows": rows, "cols": cols});
                send(&mut clients, id, &encode(HELLO, facts.to_string().as_bytes()));
                if role["role"] == "interactive" {
                    if let Some(previous) = interactive.replace(id).filter(|p| *p != id) {
                        send(&mut clients, previous, &encode(DETACHED, b""));
                    }
                    send(&mut clients, id, &encode(SNAPSHOT, &with_offset(total, &model.snapshot())));
                }
            }
            Event::Frame(id, INPUT, payload) if interactive == Some(id) && !is_probe_reply(&payload) => {
                let _ = pty.write_all(&payload);
            }
            Event::Frame(id, RESIZE, payload) if interactive == Some(id) && payload.len() == 4 => {
                let cols = u16::from_be_bytes([payload[0], payload[1]]).max(1);
                let rows = u16::from_be_bytes([payload[2], payload[3]]).max(1);
                let _ = pair.master.resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 });
                model.resize(rows, cols);
            }
            Event::Frame(id, REPLAY, payload) => {
                let Some((from, _)) = split_offset(&payload) else { continue };
                let oldest = total - log.len() as u64;
                if from < oldest {
                    let mut gap = from.to_be_bytes().to_vec();
                    gap.extend_from_slice(&oldest.to_be_bytes());
                    send(&mut clients, id, &encode(GAP, &gap));
                }
                let start = from.clamp(oldest, total);
                let bytes: Vec<u8> = log.iter().skip((start - oldest) as usize).copied().collect();
                send(&mut clients, id, &encode(OUTPUT, &with_offset(start, &bytes)));
            }
            Event::Frame(..) => {}
            Event::Gone(id) => {
                clients.remove(&id);
                if interactive == Some(id) {
                    interactive = None;
                }
            }
            Event::Exit(status) => {
                let frame = encode(EXIT, serde_json::json!({ "status": status }).to_string().as_bytes());
                for c in clients.values_mut() {
                    let _ = c.write_all(&frame);
                }
                let _ = std::fs::remove_file(socket);
                return Ok(());
            }
        }
    }
    Ok(())
}
