//! Which live mirrored session a local process belongs to: the peer of a private Unix socket
//! (the mod socket, a session-browser connection), proved by OS peer identity.

use super::*;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use tokio::net::{UnixListener, UnixStream};

pub(super) struct SocketGuard {
    path: PathBuf,
    inode: u64,
}

impl Drop for SocketGuard {
    fn drop(&mut self) {
        if fs::symlink_metadata(&self.path).is_ok_and(|m| m.ino() == self.inode) {
            let _ = fs::remove_file(&self.path);
        }
    }
}

pub(super) fn bind(name: &str) -> Result<(UnixListener, SocketGuard)> {
    let path = enrollment::hook_path().with_file_name(name);
    private_dir(path.parent().ok_or("tool socket directory unavailable")?)?;
    if let Ok(meta) = fs::symlink_metadata(&path) {
        if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } {
            return Err("unsafe tool socket".into());
        }
        fs::remove_file(&path).map_err(|_| "tool socket unavailable")?;
    }
    let listener = UnixListener::bind(&path).map_err(|_| "tool socket unavailable")?;
    let inode = fs::symlink_metadata(&path)
        .map_err(|_| "tool socket unavailable")?
        .ino();
    Ok((listener, SocketGuard { path, inode }))
}

pub(super) fn peer_pid(socket: &UnixStream) -> Result<u32> {
    match lightbulb_core::os::peer(socket) {
        Ok((uid, pid)) if uid == lightbulb_core::os::euid() => Ok(pid),
        Ok(_) => Err("tool peer is not this Mac user".into()),
        Err(_) => Err("tool peer process unavailable".into()),
    }
}

pub(super) fn parent(pid: u32) -> Result<Process> {
    process(lightbulb_core::os::parent(pid).map_err(|_| "tool peer process changed")?)
}

/// Every mirror of a session that has not ended and whose own process is still that process,
/// with its key, whatever its sharing or verification state.
fn running_mirrors(scope: &Path) -> Result<Vec<(String, Mirror)>> {
    let mut running = Vec::new();
    for entry in fs::read_dir(scope).map_err(|_| "local session state unavailable")? {
        let path = entry.map_err(|_| "local session state unavailable")?.path();
        let Some(key) = path.file_stem().and_then(|s| s.to_str()) else {
            continue;
        };
        if path.extension().is_none_or(|e| e != "json")
            || key.len() != 64
            || !key.bytes().all(|b| b.is_ascii_hexdigit())
        {
            continue;
        }
        let Ok(mirror) = private_read::<Mirror>(&path) else {
            continue;
        };
        if mirror.ended_at.is_some()
            || process(mirror.native.process.pid).ok().as_ref() != Some(&mirror.native.process)
        {
            continue;
        }
        running.push((key.to_owned(), mirror));
    }
    Ok(running)
}

/// Shared and unrestricted. (A withheld session is no source: it reads and sends nothing
/// through its tools.)
fn live(key: &str, mirror: &Mirror) -> bool {
    !super::transport::stopped(key, mirror) && mirror.native.restriction.is_none()
}

/// Every mirror of a running, shared, unrestricted session whose own process is still that process.
fn live_mirrors(scope: &Path) -> Result<Vec<Mirror>> {
    Ok(running_mirrors(scope)?
        .into_iter()
        .filter(|(key, mirror)| live(key, mirror))
        .map(|(_, mirror)| mirror)
        .collect())
}

const BROWSER_AMBIGUOUS: &str = "More than one session runs in this process (the Codex app, or one Codex \
    service for several conversations), so the Lightbulb browser cannot tell which one is asking";

/// The session a browser connection belongs to: the one in the NEAREST process, on the chain from
/// the peer upward (closest first), that runs any session at all, shared or not. That session owns
/// its descendants, so a session started inside another one browses as itself, and an Only-me or
/// unverified one is refused rather than answered as the shared session around it (never the
/// wrong session's browser). A nearest process with two sessions, or a Codex process that serves
/// many conversations, cannot say which one is asking and is refused.
pub(super) fn session_of_ancestors(scope: &Path, ancestors: &[u32]) -> Result<String> {
    let running = running_mirrors(scope)?;
    let not_shared = || crate::session_browser::NOT_SHARED.to_string();
    let Some(nearest) = ancestors.iter().find_map(|pid| {
        let here: Vec<_> = running.iter().filter(|(_, m)| m.native.process.pid == *pid).collect();
        (!here.is_empty()).then_some(here)
    }) else {
        return Err(not_shared());
    };
    match nearest.as_slice() {
        [(_, m)] if m.native.provider == "codex" && super::codex::serves_many(&m.native) => Err(BROWSER_AMBIGUOUS.into()),
        [(key, m)] if live(key, m) => m.session_id.clone().ok_or_else(not_shared),
        [_] => Err(not_shared()),
        _ => Err(BROWSER_AMBIGUOUS.into()),
    }
}

/// The session still runs and is shared (a browser connection's 30 s check).
pub(super) fn mirror_live(scope: &Path, session_id: &str) -> bool {
    live_mirrors(scope).is_ok_and(|live| live.iter().any(|m| m.session_id.as_deref() == Some(session_id)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn persisted(scope: &Path, key: &str, sid: &str, shared: bool) {
        let mut mirror = super::super::transport::tests::test_mirror("22222222-2222-4222-8222-222222222222", "gen-1");
        mirror.native.provider = "claude".into();
        mirror.session_id = Some(sid.into());
        mirror.shared = shared;
        mirror.persist(&scope.join(format!("{key}.json"))).unwrap();
    }

    #[test]
    fn a_browser_connection_finds_its_shared_session_among_its_ancestors() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        // This test process is the session (test_mirror's process); a child stands in for the CDP client.
        let key = "b0b0feed".repeat(8); // its own: pending asks are process-wide
        persisted(scope.path(), &key, "session-b", true);
        let mut client = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let me = std::process::id();
        let found = session_of_ancestors(scope.path(), &[client.id(), me]);
        let alone = session_of_ancestors(scope.path(), &[client.id()]);
        let still = mirror_live(scope.path(), "session-b");
        persisted(scope.path(), &key, "session-b", false);
        let unshared = session_of_ancestors(scope.path(), &[client.id(), me]);
        let gone = mirror_live(scope.path(), "session-b");
        client.kill().unwrap();
        client.wait().unwrap();
        assert_eq!(found.unwrap(), "session-b");
        assert_eq!(alone.unwrap_err(), crate::session_browser::NOT_SHARED);
        assert_eq!(unshared.unwrap_err(), crate::session_browser::NOT_SHARED);
        assert!(still && !gone);
    }

    #[test]
    fn two_shared_sessions_in_one_process_are_refused_as_ambiguous() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        persisted(scope.path(), &"c0c0feed".repeat(8), "session-c", true);
        persisted(scope.path(), &"d0d0feed".repeat(8), "session-d", true);
        let refused = session_of_ancestors(scope.path(), &[std::process::id()]).unwrap_err();
        assert!(refused.contains("cannot tell which"), "{refused}");
    }

    #[test]
    fn a_shared_session_beside_an_only_me_or_unverified_one_is_never_answered() {
        use std::os::unix::fs::PermissionsExt;
        for neighbour in ["only-me", "unverified"] {
            let scope = tempfile::tempdir().unwrap();
            fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
            persisted(scope.path(), &"e0e0feed".repeat(8), "session-e", true);
            let key = "f0f0feed".repeat(8);
            persisted(scope.path(), &key, "session-f", neighbour == "unverified");
            if neighbour == "unverified" {
                let path = scope.path().join(format!("{key}.json"));
                let mut mirror: Mirror = private_read(&path).unwrap();
                mirror.native.restriction = Some("runtime-unverified".into());
                mirror.persist(&path).unwrap();
            }
            // Either session's client has this same process for an ancestor: neither is answered.
            let refused = session_of_ancestors(scope.path(), &[std::process::id()]).unwrap_err();
            assert!(refused.contains("cannot tell which"), "{neighbour}: {refused}");
        }
    }

    #[test]
    fn a_codex_process_serving_many_conversations_is_refused_with_one_mirrored() {
        use std::os::unix::fs::PermissionsExt;
        let scope = tempfile::tempdir().unwrap();
        fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let path = scope.path().join(format!("{}.json", "a1a1feed".repeat(8)));
        let mut mirror = super::super::transport::tests::test_mirror("33333333-3333-4333-8333-333333333333", "gen-1");
        mirror.native.provider = "codex".into();
        mirror.session_id = Some("session-g".into());
        assert!(mirror.native.app, "the Codex app's own conversation");
        mirror.persist(&path).unwrap();
        let refused = session_of_ancestors(scope.path(), &[std::process::id()]).unwrap_err();
        assert!(refused.contains("cannot tell which"), "{refused}");
    }

    /// A Claude session mirrored on `pid` (a spawned child, so it can sit below this process).
    fn persisted_on(scope: &Path, key: &str, sid: &str, shared: bool, pid: u32) {
        let mut mirror = super::super::transport::tests::test_mirror("44444444-4444-4444-8444-444444444444", "gen-1");
        mirror.native.provider = "claude".into();
        mirror.native.process = process(pid).unwrap();
        mirror.session_id = Some(sid.into());
        mirror.shared = shared;
        mirror.persist(&scope.join(format!("{key}.json"))).unwrap();
    }

    #[test]
    fn a_session_started_inside_another_browses_as_itself_never_as_the_outer_one() {
        use std::os::unix::fs::PermissionsExt;
        let me = std::process::id();
        let mut inner = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let mut client = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        // The chain: CDP client -> inner session -> this process (the outer session).
        let chain = [client.id(), inner.id(), me];
        let resolve = |inner_shared: Option<bool>| {
            let scope = tempfile::tempdir().unwrap();
            fs::set_permissions(scope.path(), fs::Permissions::from_mode(0o700)).unwrap();
            persisted(scope.path(), &"a2a2feed".repeat(8), "outer", true);
            if let Some(shared) = inner_shared {
                persisted_on(scope.path(), &"b2b2feed".repeat(8), "inner", shared, inner.id());
            }
            session_of_ancestors(scope.path(), &chain)
        };
        let both_shared = resolve(Some(true));
        let inner_only_me = resolve(Some(false));
        let no_inner = resolve(None);
        client.kill().unwrap();
        client.wait().unwrap();
        inner.kill().unwrap();
        inner.wait().unwrap();
        assert_eq!(both_shared.unwrap(), "inner");
        assert_eq!(inner_only_me.unwrap_err(), crate::session_browser::NOT_SHARED, "never the outer session");
        assert_eq!(no_inner.unwrap(), "outer");
    }
}
