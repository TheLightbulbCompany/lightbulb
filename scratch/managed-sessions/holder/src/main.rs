//! Spike holder: one pty, one provider, a terminal model, an offset-addressed output log,
//! probe answers, and a unix socket for clients. Frames: [u8 type][u32 BE len][payload].
//! Usage: lb-holder-spike <socket> <logdir> -- <provider argv...>
use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use std::collections::{HashMap, VecDeque};
use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::sync::mpsc::channel;
use std::time::{SystemTime, UNIX_EPOCH};

const HELLO: u8 = 1;
const OUTPUT: u8 = 2;
const INPUT: u8 = 3;
const RESIZE: u8 = 4;
const EXIT: u8 = 5;
const REPLAY: u8 = 6;
const GAP: u8 = 7;
const SNAPSHOT: u8 = 8;
const DETACHED: u8 = 9;
const LOG_CAP: usize = 1 << 20;

/// Queries the provider sends and the one fixed answer the holder gives itself.
const PROBES: &[(&[u8], &[u8])] = &[
    (b"\x1b[c", b"\x1b[?62;22c"),
    (b"\x1b[0c", b"\x1b[?62;22c"),
    (b"\x1b[>0q", b"\x1bP>|xterm(lightbulb-holder)\x1b\\"),
    (b"\x1b]10;?\x1b\\", b"\x1b]10;rgb:e6e6/e6e6/e9e9\x1b\\"),
    (b"\x1b]10;?\x07", b"\x1b]10;rgb:e6e6/e6e6/e9e9\x07"),
    (b"\x1b]11;?\x1b\\", b"\x1b]11;rgb:1616/1616/1a1a\x1b\\"),
    (b"\x1b]11;?\x07", b"\x1b]11;rgb:1616/1616/1a1a\x07"),
    (b"\x1b[6n", b""), // cursor position: answered from the model
];

enum Ev {
    Output(Vec<u8>),
    Connect(u64, UnixStream),
    Frame(u64, u8, Vec<u8>),
    Gone(u64),
    ChildExit(String),
}

fn frame(t: u8, payload: &[u8]) -> Vec<u8> {
    let mut f = Vec::with_capacity(5 + payload.len());
    f.push(t);
    f.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    f.extend_from_slice(payload);
    f
}

fn now_ms() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis()
}

fn find(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

/// A client's reply to a probe (DA, XTVERSION, OSC colour, CPR, kitty flags): dropped, the holder answers.
fn is_probe_reply(b: &[u8]) -> bool {
    let s = String::from_utf8_lossy(b);
    (s.starts_with("\x1b[?") && (s.ends_with('c') || s.ends_with('u')))
        || s.starts_with("\x1bP>|")
        || s.starts_with("\x1b]10;rgb")
        || s.starts_with("\x1b]11;rgb")
        || (s.len() > 3 && s.starts_with("\x1b[") && s.ends_with('R')
            && s[2..s.len() - 1].chars().all(|c| c.is_ascii_digit() || c == ';'))
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let sock = args[1].clone();
    let logdir = std::path::PathBuf::from(&args[2]);
    let argv: Vec<String> = args[4..].to_vec();
    std::fs::create_dir_all(&logdir).unwrap();
    let mut volume = std::fs::File::create(logdir.join("volume.tsv")).unwrap();
    let mut unknown = std::fs::File::create(logdir.join("probes.log")).unwrap();

    let (mut rows, mut cols) = (40u16, 120u16);
    let pair = native_pty_system()
        .openpty(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 })
        .unwrap();
    // Read the login shell's environment once (what VS Code does), then start the provider
    // directly. Running it *through* an interactive shell runs the user's rc prompts
    // (oh-my-zsh "update? [Y/n]") in the session itself.
    let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into());
    let captured = std::process::Command::new(&shell)
        .args(["-l", "-i", "-c", "env -0"])
        .env("DISABLE_AUTO_UPDATE", "true")
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
        .map(|o| o.stdout)
        .unwrap_or_default();
    let mut cmd = CommandBuilder::new(&argv[0]);
    cmd.args(&argv[1..]);
    cmd.env_clear();
    for pair in captured.split(|b| *b == 0) {
        let pair = String::from_utf8_lossy(pair);
        if let Some((k, v)) = pair.split_once('=') {
            if !k.is_empty() && k != "SHLVL" && k != "_" && k != "PWD" && k != "OLDPWD" {
                cmd.env(k, v);
            }
        }
    }
    cmd.cwd(std::env::current_dir().unwrap());
    cmd.env("TERM", "xterm-256color");
    cmd.env("COLORTERM", "truecolor");
    if std::env::var("LANG").is_err() {
        cmd.env("LANG", "en_US.UTF-8");
    }
    let mut child = pair.slave.spawn_command(cmd).unwrap();
    drop(pair.slave);
    let provider_pid = child.process_id().unwrap_or(0);
    let mut reader = pair.master.try_clone_reader().unwrap();
    let mut writer = pair.master.take_writer().unwrap();

    let (tx, rx) = channel::<Ev>();
    {
        let tx = tx.clone();
        std::thread::spawn(move || {
            let mut buf = [0u8; 65536];
            while let Ok(n) = reader.read(&mut buf) {
                if n == 0 || tx.send(Ev::Output(buf[..n].to_vec())).is_err() {
                    break;
                }
            }
        });
    }
    {
        let tx = tx.clone();
        std::thread::spawn(move || {
            let status = child.wait().map(|s| format!("{s:?}")).unwrap_or_else(|e| e.to_string());
            let _ = tx.send(Ev::ChildExit(status));
        });
    }
    let _ = std::fs::remove_file(&sock);
    let listener = UnixListener::bind(&sock).unwrap();
    std::fs::set_permissions(&sock, std::fs::Permissions::from_mode(0o600)).unwrap();
    {
        let tx = tx.clone();
        std::thread::spawn(move || {
            for (id, conn) in (1u64..).zip(listener.incoming()) {
                let Ok(conn) = conn else { continue };
                let Ok(mut rd) = conn.try_clone() else { continue };
                let _ = tx.send(Ev::Connect(id, conn));
                let tx = tx.clone();
                std::thread::spawn(move || loop {
                    let mut head = [0u8; 5];
                    if rd.read_exact(&mut head).is_err() {
                        let _ = tx.send(Ev::Gone(id));
                        return;
                    }
                    let len = u32::from_be_bytes(head[1..5].try_into().unwrap()) as usize;
                    let mut body = vec![0u8; len];
                    if rd.read_exact(&mut body).is_err() {
                        let _ = tx.send(Ev::Gone(id));
                        return;
                    }
                    let _ = tx.send(Ev::Frame(id, head[0], body));
                });
            }
        });
    }

    let mut parser = vt100::Parser::new(rows, cols, 5000);
    let mut log: VecDeque<u8> = VecDeque::new();
    let mut total: u64 = 0;
    let mut clients: HashMap<u64, UnixStream> = HashMap::new();
    let mut interactive: Option<u64> = None;
    let started = now_ms();

    for ev in rx {
        match ev {
            Ev::Output(bytes) => {
                for (q, a) in PROBES {
                    if find(&bytes, q) {
                        let answer: Vec<u8> = if *q == b"\x1b[6n" {
                            let (r, c) = parser.screen().cursor_position();
                            format!("\x1b[{};{}R", r + 1, c + 1).into_bytes()
                        } else {
                            a.to_vec()
                        };
                        let _ = writer.write_all(&answer);
                    }
                }
                // Log queries the table does not answer, so the spike can list them.
                let s = String::from_utf8_lossy(&bytes);
                for (i, _) in s.match_indices('\x1b') {
                    let tail: String = s[i..].chars().take(16).collect();
                    let known = PROBES.iter().any(|(q, _)| tail.as_bytes().starts_with(q));
                    let query = (tail.starts_with("\x1b[") && (tail.contains('q') || (tail.starts_with("\x1b[?u") || tail.starts_with("\x1b[?996n"))))
                        || (tail.starts_with("\x1b]") && tail.contains(";?"));
                    if query && !known {
                        let _ = writeln!(unknown, "{:?}", tail);
                    }
                }
                parser.process(&bytes);
                let offset = total;
                total += bytes.len() as u64;
                log.extend(&bytes);
                while log.len() > LOG_CAP {
                    log.pop_front();
                }
                let _ = writeln!(volume, "{}\t{}", now_ms() - started, bytes.len());
                let mut payload = offset.to_be_bytes().to_vec();
                payload.extend_from_slice(&bytes);
                let f = frame(OUTPUT, &payload);
                clients.retain(|_, c| c.write_all(&f).is_ok());
            }
            Ev::Connect(id, conn) => {
                clients.insert(id, conn);
            }
            Ev::Frame(id, t, body) => match t {
                HELLO => {
                    let role: serde_json::Value = serde_json::from_slice(&body).unwrap_or_default();
                    let info = serde_json::json!({"protocol": 1, "pid": std::process::id(),
                        "providerPid": provider_pid, "argv": argv, "rows": rows, "cols": cols});
                    let Some(c) = clients.get_mut(&id) else { continue };
                    let _ = c.write_all(&frame(HELLO, info.to_string().as_bytes()));
                    if role["role"] == "interactive" {
                        if let Some(prev) = interactive.replace(id) {
                            if let Some(p) = clients.get_mut(&prev) {
                                let _ = p.write_all(&frame(DETACHED, b""));
                            }
                        }
                        let screen = parser.screen();
                        let mut snap = Vec::new();
                        if screen.alternate_screen() {
                            snap.extend_from_slice(b"\x1b[?1049h");
                        }
                        snap.extend(screen.state_formatted());
                        let mut payload = total.to_be_bytes().to_vec();
                        payload.extend(snap);
                        if let Some(c) = clients.get_mut(&id) {
                            let _ = c.write_all(&frame(SNAPSHOT, &payload));
                        }
                    }
                }
                INPUT if interactive == Some(id) && !is_probe_reply(&body) => {
                    let _ = writer.write_all(&body);
                }
                RESIZE if interactive == Some(id) && body.len() == 4 => {
                    cols = u16::from_be_bytes([body[0], body[1]]);
                    rows = u16::from_be_bytes([body[2], body[3]]);
                    let _ = pair.master.resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 });
                    parser.screen_mut().set_size(rows, cols);
                }
                REPLAY if body.len() == 8 => {
                    let from = u64::from_be_bytes(body[..8].try_into().unwrap());
                    let oldest = total - log.len() as u64;
                    let Some(c) = clients.get_mut(&id) else { continue };
                    if from < oldest {
                        let mut g = from.to_be_bytes().to_vec();
                        g.extend_from_slice(&oldest.to_be_bytes());
                        let _ = c.write_all(&frame(GAP, &g));
                    }
                    let start = from.max(oldest).min(total);
                    let bytes: Vec<u8> = log.iter().skip((start - oldest) as usize).copied().collect();
                    let mut payload = start.to_be_bytes().to_vec();
                    payload.extend(bytes);
                    let _ = c.write_all(&frame(OUTPUT, &payload));
                }
                _ => {}
            },
            Ev::Gone(id) => {
                clients.remove(&id);
                if interactive == Some(id) {
                    interactive = None;
                }
            }
            Ev::ChildExit(status) => {
                let f = frame(EXIT, serde_json::json!({ "status": status }).to_string().as_bytes());
                for c in clients.values_mut() {
                    let _ = c.write_all(&f);
                }
                let _ = std::fs::remove_file(&sock);
                return;
            }
        }
    }
}
