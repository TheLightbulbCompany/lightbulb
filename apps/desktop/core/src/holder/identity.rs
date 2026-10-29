//! Kernel identities for holders whose executable may have been removed by an updater.
//! Provider executable verification stays in local_sessions; these reads never need a path.

use crate::os;
use std::os::unix::net::UnixStream;

pub use crate::os::Process as Identity;

pub fn read(pid: u32) -> Option<Identity> {
    os::process(pid).ok()
}

/// When this user's loginwindow started (µs since the epoch): the current macOS login session's
/// start. A raw read: loginwindow's real uid is root, so `read` (own processes only) refuses it.
/// Filtered by effective uid so fast user switching finds this user's loginwindow, not another's.
pub fn login_session_start() -> Option<u64> {
    #[cfg(target_os = "macos")]
    {
        let n = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
        let mut pids = vec![0 as libc::pid_t; n.max(0) as usize + 64];
        let bytes = (pids.len() * std::mem::size_of::<libc::pid_t>()) as i32;
        let n = unsafe { libc::proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
        pids.truncate(n.max(0) as usize);
        let me = unsafe { libc::geteuid() };
        pids.into_iter()
            .filter_map(|pid| {
                let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
                let size = std::mem::size_of_val(&info) as i32;
                let ok = unsafe {
                    libc::proc_pidinfo(pid, libc::PROC_PIDTBSDINFO, 0, (&mut info as *mut libc::proc_bsdinfo).cast(), size)
                } == size;
                let comm = unsafe { std::ffi::CStr::from_ptr(info.pbi_comm.as_ptr()) };
                (ok && info.pbi_uid == me && comm.to_bytes() == b"loginwindow")
                    .then(|| info.pbi_start_tvsec.checked_mul(1_000_000)?.checked_add(info.pbi_start_tvusec))
                    .flatten()
            })
            .max()
    }
    #[cfg(not(target_os = "macos"))]
    {
        None
    }
}

/// The socket's actual peer, not a PID asserted in HELLO.
pub fn peer(socket: &UnixStream) -> Option<Identity> {
    let (uid, pid) = os::peer(socket).ok()?;
    if uid != os::euid() {
        return None;
    }
    read(pid)
}

pub fn is_parent(provider: u32, birth: u64, holder: u32) -> bool {
    checked_parent(provider, birth, holder, read)
}

fn checked_parent(
    provider: u32,
    birth: u64,
    holder: u32,
    mut read: impl FnMut(u32) -> Option<Identity>,
) -> bool {
    let Some(child) = read(provider) else {
        return false;
    };
    if child.pid != provider
        || child.birth_micros != birth
        || birth == 0
        || child.parent != holder
        || holder <= 1
    {
        return false;
    }
    let Some(parent) = read(holder) else {
        return false;
    };
    parent.pid == holder
        && parent.birth_micros != 0
        && parent.birth_micros <= birth
        && read(holder) == Some(parent)
        && read(provider) == Some(child)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identities() -> (Identity, Identity) {
        (
            Identity {
                pid: 43,
                parent: 42,
                birth_micros: 200,
            },
            Identity {
                pid: 42,
                parent: 1,
                birth_micros: 100,
            },
        )
    }

    #[test]
    #[cfg(target_os = "macos")]
    fn login_session_started_before_this_process() {
        // A CI runner's user may have no GUI login (no loginwindow): nothing to compare then.
        let Some(login) = login_session_start() else {
            eprintln!("skipped: no loginwindow for this user (no GUI login session)");
            return;
        };
        let me = read(std::process::id()).expect("own identity");
        assert!(login <= me.birth_micros, "login {login} after this process {}", me.birth_micros);
        assert!(login > 1_600_000_000_000_000, "µs since the epoch");
    }

    #[test]
    fn old_image_parent_needs_no_executable_path() {
        let (child, parent) = identities();
        // This accessor models PROC_PIDTBSDINFO alone. No executable path is available.
        assert!(checked_parent(43, 200, 42, |pid| Some(if pid == 43 {
            child
        } else {
            parent
        })));
    }

    #[test]
    fn wrong_parent_and_stale_provider_birth_are_rejected() {
        let (child, parent) = identities();
        for (birth, holder) in [(201, 42), (200, 41)] {
            assert!(!checked_parent(43, birth, holder, |pid| Some(
                if pid == 43 { child } else { parent }
            )));
        }
    }

    #[test]
    fn parent_reuse_and_reparenting_during_lookup_are_rejected() {
        let (child, parent) = identities();
        for changed in [
            Identity {
                birth_micros: 101,
                ..parent
            },
            Identity { parent: 1, ..child },
            Identity {
                birth_micros: 201,
                ..child
            },
        ] {
            let mut calls = 0;
            assert!(!checked_parent(43, 200, 42, |pid| {
                calls += 1;
                Some(if calls > 2 && pid == changed.pid {
                    changed
                } else if pid == child.pid {
                    child
                } else {
                    parent
                })
            }));
        }
    }

    #[test]
    fn reused_parent_born_after_child_is_rejected() {
        let (child, parent) = identities();
        assert!(!checked_parent(43, 200, 42, |pid| Some(if pid == 43 {
            child
        } else {
            Identity {
                birth_micros: 201,
                ..parent
            }
        })));
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn socket_peer_comes_from_kernel() {
        let (socket, _other) = UnixStream::pair().unwrap();
        assert_eq!(peer(&socket).unwrap().pid, std::process::id());
    }
}
