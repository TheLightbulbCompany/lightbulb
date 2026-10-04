//! Starting and finding holders. No registry file: a holder IS its socket, and `HELLO` says
//! what runs in it. A socket that refuses a connection belongs to a dead holder.

use super::protocol::{encode, read_frame, HELLO};
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

/// How Lightbulb runs `claude attach <job>` for a Claude background session (`local_sessions::screen`).
/// The `lightbulb-attach` name marks the holder as ours: stopping it ends only this attach client.
pub const ATTACH_SCRIPT: &str = r#"exec "$1" attach "$2""#;

pub fn attach_argv(claude: &Path, job: &str) -> Vec<String> {
    vec!["/bin/sh".into(), "-c".into(), ATTACH_SCRIPT.into(), "lightbulb-attach".into(), claude.to_string_lossy().into_owned(), job.into()]
}

/// The background job an attach holder shows; None for every other holder.
pub fn attach_job(info: &HolderInfo) -> Option<&str> {
    match info.argv.as_slice() {
        [sh, c, script, name, _, job]
            if sh == "/bin/sh" && c == "-c" && script == ATTACH_SCRIPT && name == "lightbulb-attach"
                && job.len() == 8 && job.bytes().all(|b| b.is_ascii_hexdigit()) => Some(job),
        _ => None,
    }
}

/// Stop an attach holder. Its exit closes the pty, so the `claude attach` client gets SIGHUP; the
/// background job runs on under Claude's own daemon (never `claude stop`). SIGTERM is not proven to
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
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.write_all(&encode(HELLO, br#"{"role":"recorder"}"#)).ok()?;
    let (kind, payload) = read_frame(&mut conn).ok()?;
    if kind != HELLO {
        return None;
    }
    let v: serde_json::Value = serde_json::from_slice(&payload).ok()?;
    Some(HolderInfo {
        id: socket.file_stem()?.to_string_lossy().into_owned(),
        socket: socket.to_path_buf(),
        pid: v["pid"].as_u64()? as u32,
        provider_pid: v["providerPid"].as_u64()? as u32,
        argv: serde_json::from_value(v["argv"].clone()).ok()?,
        cwd: PathBuf::from(v["cwd"].as_str()?),
        watch: v["watch"].as_bool().unwrap_or(false),
        type_input: v["type"].as_bool().unwrap_or(false),
    })
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

#[cfg(test)]
mod tests {
    use super::*;

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
}
