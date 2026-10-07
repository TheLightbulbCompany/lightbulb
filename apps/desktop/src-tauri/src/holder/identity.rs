//! Kernel identities for holders whose executable may have been removed by an updater.
//! Provider executable verification stays in local_sessions; these reads never need a path.

use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Identity {
    pub pid: u32,
    pub parent: u32,
    pub birth_micros: u64,
}

pub(crate) fn read(pid: u32) -> Option<Identity> {
    #[cfg(target_os = "macos")]
    {
        if pid <= 1 || pid > i32::MAX as u32 {
            return None;
        }
        let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
        let size = std::mem::size_of_val(&info) as i32;
        if unsafe {
            libc::proc_pidinfo(
                pid as i32,
                libc::PROC_PIDTBSDINFO,
                0,
                &mut info as *mut _ as *mut _,
                size,
            )
        } != size
            || info.pbi_pid != pid
            || info.pbi_uid != unsafe { libc::geteuid() }
            || info.pbi_ruid != unsafe { libc::getuid() }
        {
            return None;
        }
        let birth_micros = info
            .pbi_start_tvsec
            .checked_mul(1_000_000)?
            .checked_add(info.pbi_start_tvusec)?;
        (birth_micros != 0).then_some(Identity {
            pid,
            parent: info.pbi_ppid,
            birth_micros,
        })
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = pid;
        None
    }
}

/// The socket's actual peer, not a PID asserted in HELLO.
pub(crate) fn peer(socket: &UnixStream) -> Option<Identity> {
    #[cfg(target_os = "macos")]
    {
        let (mut uid, mut gid) = (0 as libc::uid_t, 0 as libc::gid_t);
        if unsafe { libc::getpeereid(socket.as_raw_fd(), &mut uid, &mut gid) } != 0
            || uid != unsafe { libc::geteuid() }
        {
            return None;
        }
        let mut pid = 0 as libc::pid_t;
        let mut len = std::mem::size_of_val(&pid) as libc::socklen_t;
        if unsafe {
            libc::getsockopt(
                socket.as_raw_fd(),
                libc::SOL_LOCAL,
                libc::LOCAL_PEERPID,
                &mut pid as *mut _ as *mut _,
                &mut len,
            )
        } != 0
            || len as usize != std::mem::size_of_val(&pid)
            || pid <= 1
        {
            return None;
        }
        read(pid as u32)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = socket;
        None
    }
}

pub(crate) fn is_parent(provider: u32, birth: u64, holder: u32) -> bool {
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
