//! Starting and finding holders. No registry file: a holder IS its socket, and `HELLO` says
//! what runs in it. A socket that refuses a connection belongs to a dead holder.

use super::protocol::{encode, read_frame, split_offset, with_offset, HELLO, OUTPUT, REPLAY, SNAPSHOT};
use serde::Serialize;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

#[derive(Clone, Debug, Serialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct HolderInfo {
    pub id: String,
    pub socket: PathBuf,
    pub pid: u32,
    pub provider_pid: u32,
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    pub watch: bool,
    /// It takes input-only `type` clients (remote typing).
    pub type_input: bool,
}

impl HolderInfo {
    /// The start id a linked start hands its `/bin/sh` wrapper (`session_start::held_argv`).
    pub fn start_id(&self) -> Option<&str> {
        let linked = self.argv.first().is_some_and(|a| a == "/bin/sh")
            && self.argv.get(3).is_some_and(|a| a == "lightbulb-start");
        linked.then(|| self.argv.get(4).map(String::as_str)).flatten()
    }
}

/// `/private/tmp/lightbulb-sessions-<uid>/h`, beside the app's other local-session sockets.
pub fn socket_dir() -> PathBuf {
    PathBuf::from(format!("/private/tmp/lightbulb-sessions-{}/h", unsafe { libc::getuid() }))
}

/// Create (or accept) the socket directory and its parent: real directories owned by this user,
/// never through a symlink another account planted at the predictable /tmp path.
pub(crate) fn ensure_dir(dir: &Path) -> Result<(), String> {
    use std::os::unix::fs::{DirBuilderExt, MetadataExt};
    let uid = unsafe { libc::getuid() };
    for d in [dir.parent().unwrap_or(dir), dir] {
        match std::fs::symlink_metadata(d) {
            Ok(meta) if meta.file_type().is_dir() && meta.uid() == uid => {}
            Ok(_) => return Err("The session folder is not safe to use.".into()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                std::fs::DirBuilder::new().mode(0o700).create(d).map_err(|e| e.to_string())?;
            }
            Err(e) => return Err(e.to_string()),
        }
        std::fs::set_permissions(d, std::fs::Permissions::from_mode(0o700)).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// Stop a holder and everything it started for itself (its login shell while starting up).
/// It leads its own process group (`setsid`), so the group id is its pid.
pub fn stop(pid: u32) {
    unsafe { libc::kill(-(pid as libc::pid_t), libc::SIGTERM) };
}

/// How Lightbulb shows a session that the provider's own daemon hosts: as one more client of it
/// (`local_sessions::screen`). The `lightbulb-attach` name marks the holder as ours: stopping it
/// ends only this client. `claude attach <job>` for a Claude background session.
pub const ATTACH_SCRIPT: &str = r#"exec "$1" attach "$2""#;
/// A plain Codex thread runs in Codex's shared app-server daemon, and `codex resume <thread>` is
/// one more TUI on it (tested 2026-10-04: turns mirror both ways, each client draws at its own
/// size, the thread runs on when a client leaves). `--remote unix://` keeps it on the daemon (no
/// "run without daemon" second writer); without the `-c`, an update prompt hides the conversation.
pub const CODEX_ATTACH_SCRIPT: &str = r#"exec "$1" resume "$2" -c check_for_update_on_startup=false --remote unix://"#;

/// Whose job this is, by its shape: a Claude background job id (8 hex) or a Codex thread id (UUID).
pub fn attach_provider(job: &str) -> Option<&'static str> {
    let hex = |s: &str| s.bytes().all(|b| b.is_ascii_hexdigit());
    match job.len() {
        8 if hex(job) => Some("claude"),
        36 if job.split('-').map(str::len).eq([8, 4, 4, 4, 12]) && job.split('-').all(hex) => Some("codex"),
        _ => None,
    }
}

/// `program`: the provider `attach_provider(job)` names.
pub fn attach_argv(program: &Path, job: &str) -> Vec<String> {
    let script = if attach_provider(job) == Some("codex") { CODEX_ATTACH_SCRIPT } else { ATTACH_SCRIPT };
    vec!["/bin/sh".into(), "-c".into(), script.into(), "lightbulb-attach".into(), program.to_string_lossy().into_owned(), job.into()]
}

/// The job an attach holder shows; None for every other holder.
pub fn attach_job(info: &HolderInfo) -> Option<&str> {
    match info.argv.as_slice() {
        [sh, c, script, name, _, job] if sh == "/bin/sh" && c == "-c" && name == "lightbulb-attach" => {
            let provider = attach_provider(job)?;
            (script == if provider == "codex" { CODEX_ATTACH_SCRIPT } else { ATTACH_SCRIPT }).then_some(job.as_str())
        }
        _ => None,
    }
}

/// Stop an attach holder. Its exit closes the pty, so the attach client gets SIGHUP; the
/// session runs on under its provider's own daemon (never `claude stop`). SIGTERM is not proven to
/// end the client (Task 0), so whatever is still alive after `KILL_AFTER` is killed. Does not block.
pub fn stop_attach(info: &HolderInfo) {
    const KILL_AFTER: Duration = Duration::from_secs(2);
    let (holder, client) = (info.pid as libc::pid_t, info.provider_pid as libc::pid_t);
    // Discovery may ask again every second: one kill thread per holder.
    static STOPPING: std::sync::Mutex<Vec<libc::pid_t>> = std::sync::Mutex::new(Vec::new());
    if holder <= 1 || STOPPING.lock().map_or(true, |mut s| s.contains(&holder) || { s.push(holder); false }) {
        return;
    }
    // Who they are now: a pid that exits during the grace period can be reused by an unrelated process.
    let was = (birth(holder), birth(client));
    stop(info.pid);
    std::thread::spawn(move || {
        std::thread::sleep(KILL_AFTER);
        if let Ok(mut s) = STOPPING.lock() {
            s.retain(|p| *p != holder);
        }
        kill_if_same(holder, client, was);
    });
}

fn birth(pid: libc::pid_t) -> Option<u64> {
    (pid > 1).then(|| crate::local_sessions::process_birth(pid as u32)).flatten()
}

/// SIGKILL each pid that is still the process it was when `was` was read; skip any that changed.
fn kill_if_same(holder: libc::pid_t, client: libc::pid_t, was: (Option<u64>, Option<u64>)) {
    let same = |pid, was: Option<u64>| was.is_some() && birth(pid) == was;
    let holder_same = same(holder, was.0);
    // The holder's group id is its pid. A free pid is never handed out while a group still uses it, so
    // once the holder is gone (ESRCH) a group with that id is its leftovers.
    // ponytail: unless the pid was reused, made a group leader and exited within the 2 s; a group start-time check if that ever matters.
    let holder_gone = holder > 1
        && unsafe { libc::kill(holder, 0) } != 0
        && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH);
    unsafe {
        if holder_same || holder_gone {
            libc::kill(-holder, libc::SIGKILL);
        }
        if holder_same {
            libc::kill(holder, libc::SIGKILL);
        }
        if same(client, was.1) {
            libc::kill(client, libc::SIGKILL);
        }
    }
}

/// Start a holder from this app's own binary (`<exe> holder <socket> -- argv`).
pub fn start(cwd: &Path, argv: &[String]) -> Result<HolderInfo, String> {
    start_sized(cwd, argv, None)
}

/// Start a holder whose terminal begins at `size` (`cols`, `rows`): the size of the terminal
/// that attaches to it first.
pub fn start_sized(cwd: &Path, argv: &[String], size: Option<(u16, u16)>) -> Result<HolderInfo, String> {
    start_sized_env(cwd, argv, size, None)
}

/// As `start_sized`, with `env` as the provider's whole environment instead of the login shell's
/// (`lightbulb claude` passes its terminal's). It reaches the holder through the holder's own
/// environment, never its argv, which `ps` shows.
pub fn start_sized_env(cwd: &Path, argv: &[String], size: Option<(u16, u16)>, env: Option<Vec<(String, String)>>) -> Result<HolderInfo, String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    start_with_env(&exe, &socket_dir(), cwd, argv, size, env)
}

pub fn start_with(exe: &Path, dir: &Path, cwd: &Path, argv: &[String], size: Option<(u16, u16)>) -> Result<HolderInfo, String> {
    start_with_env(exe, dir, cwd, argv, size, None)
}

pub fn start_with_env(exe: &Path, dir: &Path, cwd: &Path, argv: &[String], size: Option<(u16, u16)>, env: Option<Vec<(String, String)>>) -> Result<HolderInfo, String> {
    ensure_dir(dir)?;
    // Eight hex characters keep the socket path well under the 104-byte limit.
    let id = uuid::Uuid::new_v4().simple().to_string()[..8].to_owned();
    let socket = dir.join(format!("{id}.sock"));
    let mut cmd = std::process::Command::new(exe);
    cmd.arg("holder").arg(&socket);
    if let Some((cols, rows)) = size {
        cmd.arg(format!("--size={cols}x{rows}"));
    }
    cmd.arg("--").args(argv).current_dir(cwd);
    if let Some(env) = env {
        cmd.env_clear().envs(env).env(super::run::INHERIT_ENV, "1");
    }
    cmd.stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    // Its own session: no controlling terminal, untouched by the app's process group.
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    let mut child = cmd.spawn().map_err(|e| e.to_string())?;
    let pid = child.id() as libc::pid_t;
    // Reap it if it exits while this app is alive; after an app restart launchd reaps it.
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    let deadline = Instant::now() + Duration::from_secs(10);
    while Instant::now() < deadline {
        if let Some(info) = hello(&socket) {
            return Ok(info);
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    // Never leave a session running that nobody was told about.
    stop(pid as u32);
    Err("The session did not start in time. Try again.".into())
}

/// The running holders, oldest first. Removes sockets whose holder is gone.
pub fn list() -> Vec<HolderInfo> {
    list_in(&socket_dir())
}

pub fn list_in(dir: &Path) -> Vec<HolderInfo> {
    let Ok(entries) = std::fs::read_dir(dir) else { return vec![] };
    let mut found: Vec<(std::time::SystemTime, HolderInfo)> = vec![];
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().is_none_or(|e| e != "sock") {
            continue;
        }
        match hello(&path) {
            Some(info) => found.push((entry.metadata().and_then(|m| m.created()).unwrap_or(std::time::UNIX_EPOCH), info)),
            None if UnixStream::connect(&path).is_err() => {
                let _ = std::fs::remove_file(&path);
                let _ = std::fs::remove_file(path.with_extension("thread"));
            }
            None => {}
        }
    }
    found.sort_by_key(|(created, _)| *created);
    found.into_iter().map(|(_, info)| info).collect()
}

/// Connect as an output-only client and read the holder's facts.
pub fn hello(socket: &Path) -> Option<HolderInfo> {
    let mut conn = UnixStream::connect(socket).ok()?;
    let peer = super::identity::peer(&conn)?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.write_all(&encode(HELLO, br#"{"role":"recorder"}"#)).ok()?;
    let (kind, payload) = read_frame(&mut conn).ok()?;
    if kind != HELLO {
        return None;
    }
    let v: serde_json::Value = serde_json::from_slice(&payload).ok()?;
    let pid = u32::try_from(v["pid"].as_u64()?).ok()?;
    if pid != peer.pid || super::identity::read(pid) != Some(peer) { return None; }
    Some(HolderInfo {
        id: socket.file_stem()?.to_string_lossy().into_owned(),
        socket: socket.to_path_buf(),
        pid,
        provider_pid: u32::try_from(v["providerPid"].as_u64()?).ok()?,
        argv: serde_json::from_value(v["argv"].clone()).ok()?,
        cwd: PathBuf::from(v["cwd"].as_str()?),
        watch: v["watch"].as_bool().unwrap_or(false),
        type_input: v["type"].as_bool().unwrap_or(false),
    })
}

/// The holder's last `max` bytes of output for a viewer's scrollback (spec §5.2 `history`, §5.6),
/// from a line start unless they start the session. Asked on a short-lived `watch` connection, so
/// it never mixes with a relay's own stream. Empty when nothing was printed; None when the holder
/// does not answer.
pub fn history(socket: &Path, max: usize) -> Option<Vec<u8>> {
    let mut conn = UnixStream::connect(socket).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    history_on(&mut conn, max)
}

fn history_on(conn: &mut UnixStream, max: usize) -> Option<Vec<u8>> {
    conn.write_all(&encode(HELLO, br#"{"role":"watch"}"#)).ok()?;
    if read_frame(conn).ok()?.0 != HELLO {
        return None;
    }
    // A watch client's first frame is the screen, at the log's current end.
    let (kind, screen) = read_frame(conn).ok()?;
    let (end, _) = split_offset(&screen).filter(|_| kind == SNAPSHOT)?;
    if end == 0 || max == 0 {
        return Some(vec![]);
    }
    conn.write_all(&encode(REPLAY, &with_offset(end - end.min(max as u64), b""))).ok()?;
    // Live output goes on from `end`, each frame where the last ended; the answer is the one that
    // does not (it starts below and runs to where the live output is now).
    let mut next = end;
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        let (kind, payload) = read_frame(conn).ok()?;
        if kind != OUTPUT {
            continue; // GAP: the older part is gone, the answer follows.
        }
        let (from, bytes) = split_offset(&payload)?;
        if from == next {
            next += bytes.len() as u64;
            continue;
        }
        let cut = bytes.len().saturating_sub(max);
        let kept = &bytes[cut..];
        if from + cut as u64 == 0 {
            return Some(kept.to_vec());
        }
        // Mid-session: start at a line, not inside one or inside an escape sequence.
        let line = kept.iter().position(|b| *b == b'\n').map_or(0, |i| i + 1);
        return Some(kept[line..].to_vec());
    }
    None
}

/// The Codex thread this holder's provider is on, as its own `SessionStart` hook recorded it
/// (spec §5.4: a Codex rollout may be written by a shared app server, not the holder's child).
pub fn thread(info: &HolderInfo) -> Option<String> {
    let text = std::fs::read_to_string(info.socket.with_extension("thread")).ok()?;
    let thread = text.trim();
    uuid::Uuid::parse_str(thread).is_ok().then(|| thread.to_owned())
}

/// Record the thread a held Codex started. A later one (`/new`, a resume) replaces it.
pub fn bind_thread(info: &HolderInfo, thread: &str) {
    if uuid::Uuid::parse_str(thread).is_err() {
        return;
    }
    let tmp = info.socket.with_extension("thread.tmp");
    if std::fs::write(&tmp, thread).is_ok() {
        let _ = std::fs::rename(&tmp, info.socket.with_extension("thread"));
    }
}

/// When this holder's provider exited (unix ms), if it only shows the final screen now (spec
/// 2026-10-07 §5.4). An older holder never says so; one that does not answer is not ended.
pub fn ended_at(socket: &Path) -> Option<u64> {
    let mut conn = UnixStream::connect(socket).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.write_all(&encode(HELLO, br#"{"role":"origin"}"#)).ok()?;
    let (kind, facts) = read_frame(&mut conn).ok()?;
    serde_json::from_slice::<serde_json::Value>(&facts).ok().filter(|_| kind == HELLO)?["endedAt"].as_u64()
}

/// Whether this holder's provider exited and it only shows the final screen.
pub fn is_ended(info: &HolderInfo) -> bool {
    ended_at(&info.socket).is_some()
}

/// Close every holder that only shows an ended session's final screen. The app does this once at
/// launch: an ended screen lives only while the app that saw it end runs (spec §5.4).
pub fn stop_ended() {
    for holder in list() {
        if is_ended(&holder) {
            stop(holder.pid);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "macos")]
    #[test]
    fn holder_hello_rejects_a_claimed_pid_that_is_not_the_socket_peer() {
        use std::os::unix::net::UnixListener;
        for claimed in [std::process::id(), std::process::id() + 1] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("ab12cd34.sock");
            let listener = UnixListener::bind(&path).unwrap();
            let server = std::thread::spawn(move || {
                let (mut conn, _) = listener.accept().unwrap();
                assert_eq!(read_frame(&mut conn).unwrap().0, HELLO);
                let facts = serde_json::json!({"pid":claimed,"providerPid":2,"argv":[],"cwd":"/"});
                conn.write_all(&encode(HELLO, facts.to_string().as_bytes())).unwrap();
            });
            assert_eq!(hello(&path).is_some(), claimed == std::process::id());
            server.join().unwrap();
        }
    }

    #[test]
    fn holder_attach_is_recognised_only_by_lightbulbs_own_argv() {
        let info = |argv: Vec<String>| HolderInfo { id: "ab12cd34".into(), socket: "/x/ab12cd34.sock".into(), pid: 1, provider_pid: 2,
            argv, cwd: "/".into(), watch: true, type_input: true };
        let claude = Path::new("/Users/a/.local/share/claude/versions/2.1.289");
        assert_eq!(attach_job(&info(attach_argv(claude, "8ed71738"))), Some("8ed71738"));
        let typed = |a: &[&str]| info(a.iter().map(|s| s.to_string()).collect());
        assert_eq!(attach_job(&typed(&["/c/claude", "attach", "8ed71738"])), None, "`lightbulb claude attach` is a held session, not ours");
        let mut forged = attach_argv(claude, "8ed71738");
        forged[5] = "../../x".into();
        assert_eq!(attach_job(&info(forged)), None);
        // A Codex thread: `codex resume` on the shared daemon, never outside it.
        let (codex, thread) = (Path::new("/opt/homebrew/bin/codex"), "01a10845-c8f1-7fa2-844b-13790e2db151");
        let argv = attach_argv(codex, thread);
        assert_eq!(argv[2], CODEX_ATTACH_SCRIPT);
        assert!(CODEX_ATTACH_SCRIPT.contains("--remote unix://") && CODEX_ATTACH_SCRIPT.contains("check_for_update_on_startup=false"));
        assert_eq!(attach_job(&info(argv.clone())), Some(thread));
        assert_eq!((attach_provider("8ed71738"), attach_provider(thread), attach_provider("01a10845-c8f1")), (Some("claude"), Some("codex"), None));
        // One provider's script with the other's job is not ours.
        let mut crossed = argv;
        crossed[2] = ATTACH_SCRIPT.into();
        assert_eq!(attach_job(&info(crossed)), None);
        assert_eq!(attach_job(&typed(&["/c/codex", "resume", thread])), None, "`lightbulb codex resume` is a held session, not ours");
    }

    #[test]
    fn holder_attach_stop_kills_a_holder_and_client_that_ignore_sigterm() {
        // A holder that ignores SIGTERM (ignored dispositions survive exec), leading its own group.
        let mut holder = std::process::Command::new("/bin/sh");
        holder.args(["-c", "trap '' TERM; exec /bin/sleep 30"]);
        unsafe {
            holder.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        let mut holder = holder.spawn().unwrap();
        let mut client = std::process::Command::new("/bin/sh").args(["-c", "trap '' TERM HUP; exec /bin/sleep 30"]).spawn().unwrap();
        std::thread::sleep(Duration::from_millis(200)); // let both reach `sleep` with the trap set
        stop_attach(&HolderInfo { id: "ab12cd34".into(), socket: "/x/ab12cd34.sock".into(), pid: holder.id(),
            provider_pid: client.id(), argv: vec![], cwd: "/".into(), watch: true, type_input: true });
        let deadline = Instant::now() + Duration::from_secs(6);
        while (holder.try_wait().unwrap().is_none() || client.try_wait().unwrap().is_none()) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(holder.try_wait().unwrap().is_some() && client.try_wait().unwrap().is_some(), "both are gone after the fallback");
    }

    #[test]
    fn holder_attach_stop_spares_a_pid_that_is_now_another_process() {
        let mut holder = std::process::Command::new("/bin/sh");
        holder.args(["-c", "trap '' TERM; exec /bin/sleep 30"]);
        unsafe {
            holder.pre_exec(|| {
                libc::setsid();
                Ok(())
            });
        }
        let mut holder = holder.spawn().unwrap();
        let mut client = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let (h, c) = (holder.id() as libc::pid_t, client.id() as libc::pid_t);
        // Start times read at stop time that no longer match: both pids now belong to other processes.
        kill_if_same(h, c, (birth(h).map(|b| b + 1), birth(c).map(|b| b + 1)));
        std::thread::sleep(Duration::from_millis(200));
        assert!(holder.try_wait().unwrap().is_none() && client.try_wait().unwrap().is_none(), "a reused pid is never killed");
        kill_if_same(h, c, (birth(h), birth(c)));
        let deadline = Instant::now() + Duration::from_secs(3);
        while (holder.try_wait().unwrap().is_none() || client.try_wait().unwrap().is_none()) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(50));
        }
        assert!(holder.try_wait().unwrap().is_some() && client.try_wait().unwrap().is_some(), "the same processes are killed");
    }

    #[test]
    fn holder_binds_one_codex_thread_and_forgets_it_with_the_holder() {
        let dir = tempfile::tempdir().unwrap();
        let info = HolderInfo {
            id: "ab12cd34".into(),
            socket: dir.path().join("ab12cd34.sock"),
            pid: 1,
            provider_pid: 2,
            argv: vec![],
            cwd: "/".into(),
            watch: false,
            type_input: false,
        };
        assert_eq!(thread(&info), None);
        bind_thread(&info, "not a thread");
        assert_eq!(thread(&info), None, "only a thread id is recorded");
        bind_thread(&info, "11111111-1111-4111-8111-111111111111");
        bind_thread(&info, "22222222-2222-4222-8222-222222222222");
        assert_eq!(
            thread(&info).as_deref(),
            Some("22222222-2222-4222-8222-222222222222"),
            "a new thread in the same Codex (/new) replaces the old one"
        );
        std::fs::write(&info.socket, b"").unwrap(); // a socket nobody listens on
        assert!(list_in(dir.path()).is_empty());
        assert_eq!(thread(&info), None, "a dead holder's binding goes with its socket");
    }

    /// A holder that answers a watch HELLO with its screen at `end`, sends `live` output, then
    /// answers a REPLAY with `answer`; returns the `from` it was asked for.
    fn replaying(end: u64, live: &'static [u8], answer: Option<(u64, &'static [u8])>) -> (UnixStream, std::thread::JoinHandle<Option<u64>>) {
        use super::super::protocol::{split_offset, with_offset, OUTPUT, REPLAY, SNAPSHOT};
        let (mac, mut holder) = UnixStream::pair().unwrap();
        let fake = std::thread::spawn(move || {
            assert_eq!(read_frame(&mut holder).unwrap(), (HELLO, br#"{"role":"watch"}"#.to_vec()));
            holder.write_all(&encode(HELLO, b"{}")).unwrap();
            holder.write_all(&encode(SNAPSHOT, &with_offset(end, b"screen"))).unwrap();
            holder.write_all(&encode(OUTPUT, &with_offset(end, live))).unwrap();
            let (from, bytes) = answer?;
            let (kind, ask) = read_frame(&mut holder).unwrap();
            assert_eq!(kind, REPLAY);
            holder.write_all(&encode(OUTPUT, &with_offset(from, bytes))).unwrap();
            Some(split_offset(&ask).unwrap().0)
        });
        (mac, fake)
    }

    #[test]
    fn history_is_the_log_tail_before_the_screen_from_a_line_and_never_live_output() {
        // Log "one\ntwo\nthree\nfour\n" is offsets 0..19; the answer runs to the live end (23).
        let (mut mac, fake) = replaying(19, b"live", Some((9, b"hree\nfour\nlive")));
        assert_eq!(history_on(&mut mac, 10).as_deref(), Some(&b"four\nlive"[..]));
        assert_eq!(fake.join().unwrap(), Some(9), "asked from max bytes under the screen");
        let (mut mac, fake) = replaying(5, b"", Some((0, b"hi\nyo")));
        assert_eq!(history_on(&mut mac, 100).as_deref(), Some(&b"hi\nyo"[..]), "the session's start is kept whole");
        assert_eq!(fake.join().unwrap(), Some(0));
        let (mut mac, fake) = replaying(0, b"", None);
        assert_eq!(history_on(&mut mac, 100).as_deref(), Some(&b""[..]));
        assert_eq!(fake.join().unwrap(), None, "nothing printed yet: nothing asked");
        let (mut mac, mut old) = UnixStream::pair().unwrap();
        old.write_all(&encode(HELLO, b"{}")).unwrap();
        old.write_all(&encode(super::super::protocol::OUTPUT, &super::super::protocol::with_offset(0, b"x"))).unwrap();
        assert_eq!(history_on(&mut mac, 100), None, "a holder older than watch clients sends no screen first");
    }
}
