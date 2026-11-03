//! Starting and finding holders. No registry file: a holder IS its socket, and `HELLO` says
//! what runs in it. Failed probes do not prove a holder is dead and never remove its endpoint.

use super::protocol::{encode, read_frame, split_offset, with_offset, END, HELLO, HISTORY, OUTPUT, REPLAY, SNAPSHOT};
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
pub fn ensure_dir(dir: &Path) -> Result<(), String> {
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

/// Holder id -> when its person asked it to end (unix s): the panel's End, and the owner's End
/// from another device (`end`). A session no holder runs is under its session id (`end_asked_now`).
// ponytail: this app run only; an end reported after a relaunch is a plain "exited" (it stays listed).
static END_ASKED: std::sync::Mutex<std::collections::BTreeMap<String, u64>> = std::sync::Mutex::new(std::collections::BTreeMap::new());

/// When this holder's person asked it to end (`end`), if they did.
pub fn end_asked(id: &str) -> Option<u64> {
    END_ASKED.lock().ok()?.get(id).copied()
}

/// Its person asks `id` to end, now: the end that follows is one they asked for.
pub fn end_asked_now(id: &str) {
    if let Ok(mut asked) = END_ASKED.lock() {
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_secs());
        asked.entry(id.to_owned()).or_insert(now);
    }
}

/// That ask is over: the stop was never sent, its end was told, or the session runs again. An
/// end after this is not one its person asked for.
pub fn end_asked_forget(id: &str) {
    if let Ok(mut asked) = END_ASKED.lock() {
        asked.remove(id);
    }
}

/// End the session a holder runs and keep its last picture: the holder ends its provider itself
/// (END) and then shows the final screen, as after `/exit`. A holder from a build before END is
/// stopped, as End always did; its screen goes with it (false). Blocks for up to 2 s.
pub fn end(info: &HolderInfo) -> bool {
    end_asked_now(&info.id);
    let kept = ask_end(&info.socket);
    if !kept {
        stop(info.pid);
    }
    kept
}

/// True when the holder takes END and was sent one.
fn ask_end(socket: &Path) -> bool {
    let ask = || {
        let mut conn = UnixStream::connect(socket).ok()?;
        conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
        conn.write_all(&encode(HELLO, br#"{"role":"origin"}"#)).ok()?;
        let (kind, facts) = read_frame(&mut conn).ok()?;
        let facts = serde_json::from_slice::<serde_json::Value>(&facts).ok().filter(|_| kind == HELLO)?;
        (facts["end"] == true).then(|| conn.write_all(&encode(END, b"")).ok()).flatten()
    };
    ask().is_some()
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
    (pid > 1).then(|| crate::os::process_birth(pid as u32)).flatten()
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

/// The running holders, oldest first. Failed probes leave endpoints alone: a busy
/// listening socket can refuse a connection, so failure does not prove its holder died.
pub fn list() -> Vec<HolderInfo> {
    list_in(&socket_dir())
}

pub fn list_in(dir: &Path) -> Vec<HolderInfo> {
    list_with_probe(dir, |path| Ok(hello(path)))
}

fn list_with_probe(dir: &Path, probe: impl Fn(&Path) -> std::io::Result<Option<HolderInfo>>) -> Vec<HolderInfo> {
    let Ok(entries) = std::fs::read_dir(dir) else { return vec![] };
    let mut found: Vec<(std::time::SystemTime, HolderInfo)> = vec![];
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().is_none_or(|e| e != "sock") {
            continue;
        }
        if let Ok(Some(info)) = probe(&path) {
            found.push((entry.metadata().and_then(|m| m.created()).unwrap_or(std::time::UNIX_EPOCH), info));
        }
    }
    found.sort_by_key(|(created, _)| *created);
    found.into_iter().map(|(_, info)| info).collect()
}

/// Connect as an output-only client and read the holder's facts.
/// Holders whose HELLO said `"resize": true`: they size the pty to the app's panel while it is the
/// latest user (`run::latest_user`). A holder started by an older build keeps running after the app
/// updates and says nothing, so its session is never reported resizable.
// ponytail: an id stays after its holder is gone (a few bytes each, ids are never reused).
static RESIZE_HOLDERS: std::sync::Mutex<std::collections::BTreeSet<String>> = std::sync::Mutex::new(std::collections::BTreeSet::new());

/// Whether the holder `id` said, at its last HELLO, that it takes the app's panel size.
pub fn takes_resize(id: &str) -> bool {
    RESIZE_HOLDERS.lock().is_ok_and(|ids| ids.contains(id))
}

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
    let id = socket.file_stem()?.to_string_lossy().into_owned();
    if let Ok(mut ids) = RESIZE_HOLDERS.lock() {
        if v["resize"] == true { ids.insert(id.clone()) } else { ids.remove(&id) };
    }
    Some(HolderInfo {
        id,
        socket: socket.to_path_buf(),
        pid,
        provider_pid: u32::try_from(v["providerPid"].as_u64()?).ok()?,
        argv: serde_json::from_value(v["argv"].clone()).ok()?,
        cwd: PathBuf::from(v["cwd"].as_str()?),
        watch: v["watch"].as_bool().unwrap_or(false),
        type_input: v["type"].as_bool().unwrap_or(false),
    })
}

/// What goes above the screen in a new viewer's terminal (spec §5.2 `history`, §5.6), at most `max`
/// bytes: the lines above the holder's own screen when its HELLO says `"history": true`; else the
/// holder's last output, from a line start unless it starts the session. Asked on a short-lived
/// `watch` connection, so it never mixes with a relay's own stream. Empty when there is nothing;
/// None when the holder does not answer.
pub fn history(socket: &Path, max: usize) -> Option<Vec<u8>> {
    let mut conn = UnixStream::connect(socket).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    history_on(&mut conn, max)
}

fn history_on(conn: &mut UnixStream, max: usize) -> Option<Vec<u8>> {
    conn.write_all(&encode(HELLO, br#"{"role":"watch"}"#)).ok()?;
    let (kind, facts) = read_frame(conn).ok()?;
    if kind != HELLO {
        return None;
    }
    // A watch client's first frame is the screen, at the log's current end.
    let (kind, screen) = read_frame(conn).ok()?;
    let (end, _) = split_offset(&screen).filter(|_| kind == SNAPSHOT)?;
    if serde_json::from_slice::<serde_json::Value>(&facts).is_ok_and(|facts| facts["history"] == true) {
        conn.write_all(&encode(HISTORY, &with_offset(max as u64, b""))).ok()?;
        // Live output may come first; a holder that never answers runs into the read timeout.
        loop {
            let (kind, lines) = read_frame(conn).ok()?;
            if kind == HISTORY {
                return Some(lines);
            }
        }
    }
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
    thread_at(&info.socket.with_extension("thread"))
}

/// The Codex thread holder `id` in `dir` is on (`thread`), read from its one small file without
/// asking the holder: what an End checks before it ends a held Codex.
pub fn bound_thread(dir: &Path, id: &str) -> Option<String> {
    thread_at(&dir.join(format!("{id}.thread")))
}

fn thread_at(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
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
    ended(socket).map(|(at, _)| at)
}

/// `ended_at`, and whether the provider left by itself with status 0 (`/exit`, quit): a crash, a
/// kill or a hang-up is never 0. A holder from a build before `endedCode` never says so.
pub fn ended(socket: &Path) -> Option<(u64, bool)> {
    let facts = facts(socket)?;
    Some((facts["endedAt"].as_u64()?, facts["endedCode"] == 0))
}

/// The holder's HELLO facts, asked on a short-lived `origin` connection (it gets nothing else).
fn facts(socket: &Path) -> Option<serde_json::Value> {
    facts_asking(socket, br#"{"role":"origin"}"#)
}

/// An idle stop: the holder ends its provider (and keeps its last picture, as after END) only if
/// no terminal or panel has its keyboard when it reads this, and says whether it did. Never a
/// stop: a holder that does not answer, or refuses, runs on. Blocks for up to 2 s.
pub fn end_idle(info: &HolderInfo) -> bool {
    facts_asking(&info.socket, br#"{"role":"origin","idleEnd":true}"#).is_some_and(|f| f["idleEnded"] == true)
}

fn facts_asking(socket: &Path, hello: &[u8]) -> Option<serde_json::Value> {
    let mut conn = UnixStream::connect(socket).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.write_all(&encode(HELLO, hello)).ok()?;
    let (kind, facts) = read_frame(&mut conn).ok()?;
    serde_json::from_slice::<serde_json::Value>(&facts).ok().filter(|_| kind == HELLO)
}

/// Held sessions no terminal and no app panel has the keyboard of: what an idle stop may end
/// (`transport::Host::stop_idle`). Never an attach holder (a view onto the provider's own daemon)
/// or an ended one. A holder from a build before `"attached"` never says so, and is left be.
pub fn unattached(holders: &[HolderInfo]) -> Vec<HolderInfo> {
    holders
        .iter()
        .filter(|h| attach_job(h).is_none() && facts(&h.socket).is_some_and(|f| f["attached"] == false && f["ended"] != true))
        .cloned()
        .collect()
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

/// Holders born before this login session: their provider lost the login's services (DNS, the
/// keychain, `id -un`) for good while it still reports Idle (proved 2026-10-11). Attach holders are
/// views onto a provider's own daemon, not sessions; ended ones are already kept pictures.
pub fn stranded(holders: &[HolderInfo]) -> Vec<HolderInfo> {
    let Some(login) = super::identity::login_session_start() else { return vec![] };
    stranded_at(holders, login, |pid| super::identity::read(pid).map(|me| me.birth_micros))
        .into_iter()
        .filter(|h| !is_ended(h))
        .collect()
}

/// `stranded` without the I/O: `login` and each pid's birth in µs. An unknown birth is not stranded.
pub fn stranded_at(holders: &[HolderInfo], login: u64, birth: impl Fn(u32) -> Option<u64>) -> Vec<HolderInfo> {
    holders
        .iter()
        .filter(|h| attach_job(h).is_none() && birth(h.pid).is_some_and(|b| b < login))
        .cloned()
        .collect()
}

#[cfg(test)]
mod tests {
    #[test]
    fn holder_discovery_ignores_refused_endpoints_without_deleting_their_identity() {
        let dir = tempfile::tempdir().unwrap();
        let socket = dir.path().join("12345678.sock");
        let thread = socket.with_extension("thread");
        std::fs::write(&socket, b"endpoint").unwrap();
        std::fs::write(&thread, b"thread identity").unwrap();
        assert!(list_with_probe(dir.path(), |_| Err(std::io::ErrorKind::ConnectionRefused.into())).is_empty());
        assert!(socket.exists());
        assert!(thread.exists());
    }

    #[test]
    fn holder_discovery_keeps_endpoints_when_a_probe_is_denied_or_interrupted() {
        use std::io::ErrorKind;
        for kind in [ErrorKind::PermissionDenied, ErrorKind::TimedOut, ErrorKind::Interrupted,
            ErrorKind::WouldBlock, ErrorKind::NotFound, ErrorKind::ConnectionRefused, ErrorKind::Other] {
            let dir = tempfile::tempdir().unwrap();
            let socket = dir.path().join("12345678.sock");
            let thread = socket.with_extension("thread");
            let listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
            std::fs::write(&thread, b"thread identity").unwrap();
            assert!(list_with_probe(dir.path(), |_| Err(std::io::Error::from(kind))).is_empty());
            assert!(socket.exists(), "{kind:?} is not proof that a holder died");
            assert!(thread.exists(), "keep the holder's conversation binding");
            let client = UnixStream::connect(&socket).unwrap();
            let (_accepted, _) = listener.accept().unwrap();
            drop(client);
        }
    }

    use super::*;

    #[test]
    fn stranded_skips_attach_holders_and_ended_ones_and_keeps_the_old() {
        // Never pid 0 or 1: `stop(pid)` signals a process group.
        let old = HolderInfo { id: "old00001".into(), socket: "/x/old00001.sock".into(), pid: 2_147_483_000, provider_pid: 2_147_483_001,
            argv: vec!["/bin/sh".into(), "-c".into(), "exec claude".into()], cwd: "/".into(), watch: true, type_input: true };
        // A real attach holder's argv: a view onto the provider's daemon, not a session.
        let attach = HolderInfo { id: "attach01".into(), argv: attach_argv(Path::new("/usr/local/bin/claude"), "ab12cd34"), ..old.clone() };
        assert!(attach_job(&attach).is_some());
        let young = HolderInfo { id: "young001".into(), pid: 2_147_483_002, ..old.clone() };
        let unknown = HolderInfo { id: "gone0001".into(), pid: 2_147_483_003, ..old.clone() };
        let birth = |pid: u32| match pid { 2_147_483_002 => Some(2_000), 2_147_483_003 => None, _ => Some(500) };
        let got = stranded_at(&[old.clone(), attach, young, unknown], 1_000, birth);
        assert_eq!(got.iter().map(|h| h.id.as_str()).collect::<Vec<_>>(), vec![old.id.as_str()]);
    }

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
    fn holder_ended_says_its_provider_left_by_itself_only_for_status_zero() {
        use std::os::unix::net::UnixListener;
        for (facts, expected) in [
            (serde_json::json!({"ended":true,"endedAt":5,"endedCode":0}), Some((5, true))),
            (serde_json::json!({"ended":true,"endedAt":5,"endedCode":1}), Some((5, false))),
            // A holder from a build before `endedCode`.
            (serde_json::json!({"ended":true,"endedAt":5}), Some((5, false))),
            (serde_json::json!({"ended":false,"endedAt":null,"endedCode":null}), None),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("ab12cd34.sock");
            let listener = UnixListener::bind(&path).unwrap();
            let server = std::thread::spawn(move || {
                let (mut conn, _) = listener.accept().unwrap();
                assert_eq!(read_frame(&mut conn).unwrap().0, HELLO);
                conn.write_all(&encode(HELLO, facts.to_string().as_bytes())).unwrap();
            });
            assert_eq!(ended(&path), expected);
            server.join().unwrap();
        }
    }

    #[test]
    fn unattached_is_only_a_live_session_that_says_no_keyboard_is_taken() {
        use std::os::unix::net::UnixListener;
        let dir = tempfile::tempdir().unwrap();
        let session = |id: &str| HolderInfo { id: id.into(), socket: dir.path().join(format!("{id}.sock")), pid: 2_147_483_000,
            provider_pid: 2_147_483_001, argv: vec!["/bin/sh".into(), "-c".into(), "exec claude".into()], cwd: "/".into(), watch: true, type_input: true };
        let mut servers = vec![];
        let mut holders = vec![];
        for (id, facts) in [
            ("free0001", serde_json::json!({"attached":false,"ended":false})),
            // A terminal (`lightbulb claude`) or the app's panel has the keyboard.
            ("term0001", serde_json::json!({"attached":true,"ended":false})),
            // A holder from a build before `attached`: never stopped.
            ("old00001", serde_json::json!({"ended":false})),
            // Already only the final screen.
            ("done0001", serde_json::json!({"attached":false,"ended":true})),
        ] {
            let holder = session(id);
            let listener = UnixListener::bind(&holder.socket).unwrap();
            servers.push(std::thread::spawn(move || {
                let (mut conn, _) = listener.accept().unwrap();
                assert_eq!(read_frame(&mut conn).unwrap(), (HELLO, br#"{"role":"origin"}"#.to_vec()));
                conn.write_all(&encode(HELLO, facts.to_string().as_bytes())).unwrap();
            }));
            holders.push(holder);
        }
        // An attach holder is a view onto the provider's daemon: never asked, never stopped.
        holders.push(HolderInfo { id: "attach01".into(), argv: attach_argv(Path::new("/usr/local/bin/claude"), "ab12cd34"), ..session("attach01") });
        let got = unattached(&holders);
        assert_eq!(got.iter().map(|h| h.id.as_str()).collect::<Vec<_>>(), vec!["free0001"]);
        for server in servers {
            server.join().unwrap();
        }
    }

    /// The owner's Mac, 2026-10-07: holder 737019b6, started 2026-10-04 by an older build, says
    /// exactly this (no `origin`, `lastInput`, `end`). It is a holder like any other: it serves
    /// watch clients, so its session is held; only End differs (no END, so it is stopped).
    #[cfg(target_os = "macos")]
    #[test]
    fn holder_from_an_older_build_is_still_a_holder_that_serves_watch_clients() {
        use std::os::unix::net::UnixListener;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("737019b6.sock");
        let listener = UnixListener::bind(&path).unwrap();
        let server = std::thread::spawn(move || {
            let mut frames = vec![];
            for _ in 0..2 {
                let (mut conn, _) = listener.accept().unwrap();
                assert_eq!(read_frame(&mut conn).unwrap().0, HELLO);
                let facts = serde_json::json!({"protocol":1,"pid":std::process::id(),"providerPid":63976,
                    "argv":["/bin/sh","-c","…","lightbulb-start","ced8346d-3b2a-4a09-966b-6ebf14e11638","scope","/claude/versions/2.1.289"],
                    "cwd":"/w","rows":43,"cols":124,"watch":true,"type":true,"joined":false});
                conn.write_all(&encode(HELLO, facts.to_string().as_bytes())).unwrap();
                conn.set_read_timeout(Some(Duration::from_millis(300))).unwrap();
                frames.push(read_frame(&mut conn).ok().map(|(kind, _)| kind));
            }
            frames
        });
        let info = hello(&path).expect("an older holder is listed");
        assert!(info.watch && info.type_input && info.provider_pid == 63976);
        assert!(!takes_resize("737019b6"), "an older holder ignores the panel's size: never reported resizable");
        assert_eq!(info.start_id(), Some("ced8346d-3b2a-4a09-966b-6ebf14e11638"));
        assert!(!ask_end(&path), "it does not take END: the caller stops it instead");
        assert_eq!(server.join().unwrap(), [None, None], "and is never sent a frame it does not know");
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
        // The attach client of a Claude background session is started with the classic-renderer
        // switch like every Claude Code process a holder runs; Codex's is not. (Claude Code draws
        // an attached background session fullscreen whatever the variable says: its docs, 2026-10-07.)
        let classic = |argv: &[String]| crate::holder::run::provider_environment(vec![], argv).iter().any(|(k, _)| k == crate::holder::run::CLASSIC_RENDERER);
        assert!(classic(&attach_argv(claude, "8ed71738")) && !classic(&argv));
        assert_eq!((attach_provider("8ed71738"), attach_provider(thread), attach_provider("01a10845-c8f1")), (Some("claude"), Some("codex"), None));
        // One provider's script with the other's job is not ours.
        let mut crossed = argv;
        crossed[2] = ATTACH_SCRIPT.into();
        assert_eq!(attach_job(&info(crossed)), None);
        assert_eq!(attach_job(&typed(&["/c/codex", "resume", thread])), None, "`lightbulb codex resume` is a held session, not ours");
    }

    // macOS until the Linux port (plan PR 8): stopping reads process births through `os`.
    #[cfg(target_os = "macos")]
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

    // macOS until the Linux port (plan PR 8): stopping reads process births through `os`.
    #[cfg(target_os = "macos")]
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
    fn holder_binds_one_codex_thread_and_keeps_it_after_failed_discovery() {
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
        std::fs::write(&info.socket, b"").unwrap();
        assert!(list_with_probe(dir.path(), |_| Err(std::io::ErrorKind::ConnectionRefused.into())).is_empty());
        assert_eq!(thread(&info).as_deref(), Some("22222222-2222-4222-8222-222222222222"),
            "a failed probe cannot invalidate a holder's conversation binding");
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
    fn history_is_the_holders_own_lines_when_it_keeps_them() {
        use super::super::protocol::{split_offset, with_offset, OUTPUT, SNAPSHOT};
        let (mut mac, mut holder) = UnixStream::pair().unwrap();
        let fake = std::thread::spawn(move || {
            assert_eq!(read_frame(&mut holder).unwrap(), (HELLO, br#"{"role":"watch"}"#.to_vec()));
            holder.write_all(&encode(HELLO, br#"{"history":true}"#)).unwrap();
            holder.write_all(&encode(SNAPSHOT, &with_offset(19, b"screen"))).unwrap();
            holder.write_all(&encode(OUTPUT, &with_offset(19, b"live"))).unwrap();
            let (kind, ask) = read_frame(&mut holder).unwrap();
            assert_eq!(kind, HISTORY, "never the output log: a classic renderer's log does not start clean in the middle");
            holder.write_all(&encode(HISTORY, b"line 1\r\nline 2\r\n")).unwrap();
            split_offset(&ask).unwrap().0
        });
        assert_eq!(history_on(&mut mac, 4096).as_deref(), Some(&b"line 1\r\nline 2\r\n"[..]));
        assert_eq!(fake.join().unwrap(), 4096, "asked for at most what the relay may send");
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
