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
use std::sync::mpsc::{channel, sync_channel, SyncSender, TrySendError};
use std::time::{Duration, Instant};

/// Output kept for `REPLAY`. Older output is answered with `GAP`.
pub const LOG_CAP: usize = 1 << 20;
const ROWS: u16 = 40;
const COLS: u16 = 120;
/// Frames queued for one client before it counts as stalled and is disconnected.
const CLIENT_QUEUE: usize = 1024;
/// How long to keep reading output after the provider exits.
const EXIT_DRAIN: Duration = Duration::from_secs(1);

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
    /// The pty reader stopped: no more output.
    Eof,
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
        let _ = out_tx.send(Event::Eof);
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
    // Connected but not yet introduced: they get nothing until their HELLO is answered.
    let mut pending: HashMap<u64, UnixStream> = HashMap::new();
    let mut clients: HashMap<u64, Client> = HashMap::new();
    let mut interactive: Option<u64> = None;
    let mut exited: Option<(String, Instant)> = None;

    loop {
        let event = match &exited {
            // The provider is gone: finish once its last output is read (or after a moment,
            // when a leftover child still holds the terminal open).
            Some((_, at)) => match rx.recv_timeout(EXIT_DRAIN.saturating_sub(at.elapsed())) {
                Ok(event) => event,
                Err(_) => Event::Eof,
            },
            None => match rx.recv() {
                Ok(event) => event,
                Err(_) => return Ok(()),
            },
        };
        match event {
            Event::Output(bytes) => {
                let answers = model.feed(&bytes);
                if !answers.is_empty() && exited.is_none() {
                    let _ = pty.write_all(&answers);
                }
                let frame = encode(OUTPUT, &with_offset(total, &bytes));
                total += bytes.len() as u64;
                log.extend(&bytes);
                let excess = log.len().saturating_sub(LOG_CAP);
                log.drain(..excess);
                clients.retain(|_, c| c.send(frame.clone()));
            }
            Event::Connect(id, conn) => {
                pending.insert(id, conn);
            }
            Event::Frame(id, HELLO, payload) => {
                let Some(conn) = pending.remove(&id) else { continue };
                let Some(client) = Client::start(conn) else { continue };
                let role = serde_json::from_slice::<serde_json::Value>(&payload).unwrap_or_default();
                let (rows, cols) = model.size();
                let facts = serde_json::json!({"protocol": 1, "pid": std::process::id(),
                    "providerPid": provider_pid, "argv": argv, "cwd": cwd, "rows": rows, "cols": cols});
                if !client.send(encode(HELLO, facts.to_string().as_bytes())) {
                    continue;
                }
                if role["role"] == "interactive" {
                    if let Some(previous) = interactive.replace(id).filter(|p| *p != id) {
                        if let Some(c) = clients.get(&previous) {
                            c.send(encode(DETACHED, b""));
                        }
                    }
                    if !client.send(encode(SNAPSHOT, &with_offset(total, &model.snapshot()))) {
                        continue;
                    }
                }
                clients.insert(id, client);
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
                let (Some((from, _)), Some(client)) = (split_offset(&payload), clients.get(&id)) else { continue };
                let oldest = total - log.len() as u64;
                if from < oldest {
                    let mut gap = from.to_be_bytes().to_vec();
                    gap.extend_from_slice(&oldest.to_be_bytes());
                    client.send(encode(GAP, &gap));
                }
                let start = from.clamp(oldest, total);
                let bytes: Vec<u8> = log.iter().skip((start - oldest) as usize).copied().collect();
                client.send(encode(OUTPUT, &with_offset(start, &bytes)));
            }
            Event::Frame(..) => {}
            Event::Gone(id) => {
                pending.remove(&id);
                clients.remove(&id);
                if interactive == Some(id) {
                    interactive = None;
                }
            }
            Event::Exit(status) => {
                exited = Some((status, Instant::now()));
            }
            Event::Eof => {
                if exited.is_none() {
                    // The terminal closed first; the exit status follows from the wait thread.
                    continue;
                }
                let status = exited.take().map(|(status, _)| status).unwrap_or_default();
                let frame = encode(EXIT, serde_json::json!({ "status": status }).to_string().as_bytes());
                for client in clients.values() {
                    client.send(frame.clone());
                }
                // Let the writers deliver the last frames before the process ends.
                drop(clients);
                std::thread::sleep(Duration::from_millis(100));
                let _ = std::fs::remove_file(socket);
                return Ok(());
            }
        }
    }
}

/// A connected client. Frames go through a bounded queue to its own writer thread, so a window
/// that stops reading can never block the holder; when its queue fills it is disconnected.
struct Client {
    queue: SyncSender<Vec<u8>>,
    stream: UnixStream,
}

impl Client {
    fn start(stream: UnixStream) -> Option<Self> {
        let mut writer = stream.try_clone().ok()?;
        let (queue, frames) = sync_channel::<Vec<u8>>(CLIENT_QUEUE);
        std::thread::spawn(move || {
            for frame in frames {
                if writer.write_all(&frame).is_err() {
                    return;
                }
            }
        });
        Some(Self { queue, stream })
    }

    /// False when the client is gone or too slow; the caller drops it.
    fn send(&self, frame: Vec<u8>) -> bool {
        match self.queue.try_send(frame) {
            Ok(()) => true,
            Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
                let _ = self.stream.shutdown(std::net::Shutdown::Both);
                false
            }
        }
    }
}

