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
}

/// `/private/tmp/lightbulb-sessions-<uid>/h`, beside the app's other local-session sockets.
pub fn socket_dir() -> PathBuf {
    PathBuf::from(format!("/private/tmp/lightbulb-sessions-{}/h", unsafe { libc::getuid() }))
}

/// Create (or accept) the socket directory and its parent: real directories owned by this user,
/// never through a symlink another account planted at the predictable /tmp path.
fn ensure_dir(dir: &Path) -> Result<(), String> {
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

/// Start a holder from this app's own binary (`<exe> holder <socket> -- argv`).
pub fn start(cwd: &Path, argv: &[String]) -> Result<HolderInfo, String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    start_with(&exe, &socket_dir(), cwd, argv)
}

pub fn start_with(exe: &Path, dir: &Path, cwd: &Path, argv: &[String]) -> Result<HolderInfo, String> {
    ensure_dir(dir)?;
    // Eight hex characters keep the socket path well under the 104-byte limit.
    let id = uuid::Uuid::new_v4().simple().to_string()[..8].to_owned();
    let socket = dir.join(format!("{id}.sock"));
    let mut cmd = std::process::Command::new(exe);
    cmd.arg("holder").arg(&socket).arg("--").args(argv).current_dir(cwd);
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
    fn holder_binds_one_codex_thread_and_forgets_it_with_the_holder() {
        let dir = tempfile::tempdir().unwrap();
        let info = HolderInfo {
            id: "ab12cd34".into(),
            socket: dir.path().join("ab12cd34.sock"),
            pid: 1,
            provider_pid: 2,
            argv: vec![],
            cwd: "/".into(),
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
