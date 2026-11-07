//! Kernel reads about local processes and socket peers: the one place that calls them.
//! macOS only. Every other OS returns `Err`, so each caller's identity check fails closed.

use std::os::fd::AsRawFd;
use std::path::PathBuf;

pub type Result<T> = std::result::Result<T, &'static str>;

/// A process is its pid and start time (its identity across pid reuse), plus its parent.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Process {
    pub pid: u32,
    pub parent: u32,
    pub birth_micros: u64,
}

/// `pid`, if it is one of this user's processes (effective and real uid). No executable
/// path is read, so a holder whose binary an updater removed still answers.
pub fn process(pid: u32) -> Result<Process> {
    #[cfg(target_os = "macos")]
    {
        if pid <= 1 || pid > i32::MAX as u32 {
            return Err("invalid process");
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
            return Err("process unavailable");
        }
        let birth_micros = info
            .pbi_start_tvsec
            .checked_mul(1_000_000)
            .and_then(|s| s.checked_add(info.pbi_start_tvusec))
            .filter(|&b| b != 0)
            .ok_or("process unavailable")?;
        Ok(Process { pid, parent: info.pbi_ppid, birth_micros })
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = pid;
        Err("local sharing supports macOS only")
    }
}

/// The parent pid of this user's process `pid`.
pub fn parent(pid: u32) -> Result<u32> {
    process(pid).map(|p| p.parent)
}

/// When `pid` started (microseconds), if it is one of this user's processes.
pub fn process_birth(pid: u32) -> Option<u64> {
    process(pid).ok().map(|p| p.birth_micros)
}

/// The executable `pid` runs, by the name macOS last looked it up under.
pub fn executable(pid: u32) -> Result<PathBuf> {
    #[cfg(target_os = "macos")]
    {
        if pid == 0 || pid > i32::MAX as u32 {
            return Err("invalid process");
        }
        let mut buffer = [0u8; 4096];
        let n = unsafe {
            libc::proc_pidpath(
                pid as i32,
                buffer.as_mut_ptr() as *mut _,
                buffer.len() as u32,
            )
        };
        if n <= 0 {
            return Err("process executable unavailable");
        }
        let executable = std::str::from_utf8(&buffer[..n as usize])
            .map_err(|_| "invalid executable path")?
            .trim_end_matches('\0');
        Ok(executable.into())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = pid;
        Err("local sharing supports macOS only")
    }
}

/// The kernel's `(uid, pid)` for the other end of a Unix socket, not anything it asserts.
/// Callers compare the uid with their own.
pub fn peer(socket: &impl AsRawFd) -> Result<(u32, u32)> {
    #[cfg(target_os = "macos")]
    {
        let fd = socket.as_raw_fd();
        let (mut uid, mut gid) = (0 as libc::uid_t, 0 as libc::gid_t);
        if unsafe { libc::getpeereid(fd, &mut uid, &mut gid) } != 0 {
            return Err("peer user unavailable");
        }
        let mut pid = 0 as libc::pid_t;
        let mut len = std::mem::size_of::<libc::pid_t>() as libc::socklen_t;
        if unsafe {
            libc::getsockopt(
                fd,
                libc::SOL_LOCAL,
                libc::LOCAL_PEERPID,
                &mut pid as *mut _ as *mut libc::c_void,
                &mut len,
            )
        } != 0
            || len as usize != std::mem::size_of::<libc::pid_t>()
            || pid <= 0
        {
            return Err("peer process unavailable");
        }
        Ok((uid, pid as u32))
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = socket;
        Err("local sharing supports macOS only")
    }
}

/// This process's effective uid, to compare with `peer`'s.
pub fn euid() -> u32 {
    #[cfg(target_os = "macos")]
    {
        unsafe { libc::geteuid() }
    }
    #[cfg(not(target_os = "macos"))]
    {
        u32::MAX
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixStream;

    #[test]
    fn invalid_pids_are_refused() {
        for pid in [0, 1, u32::MAX] {
            assert!(process(pid).is_err(), "{pid}");
            assert!(parent(pid).is_err(), "{pid}");
            assert_eq!(process_birth(pid), None, "{pid}");
        }
        assert!(executable(0).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn own_process_and_socket_peer_come_from_the_kernel() {
        let me = process(std::process::id()).unwrap();
        assert_eq!(me.pid, std::process::id());
        assert_eq!(me, process(me.pid).unwrap(), "stable across reads");
        assert_eq!(parent(me.pid).unwrap(), me.parent);
        assert_eq!(process_birth(me.pid), Some(me.birth_micros));
        assert!(executable(me.pid).unwrap().is_absolute());
        let (socket, _other) = UnixStream::pair().unwrap();
        assert_eq!(peer(&socket).unwrap(), (euid(), me.pid));
    }

    #[cfg(not(target_os = "macos"))]
    #[test]
    fn other_systems_fail_closed() {
        let me = std::process::id();
        assert!(process(me).is_err());
        assert!(executable(me).is_err());
        let (socket, _other) = UnixStream::pair().unwrap();
        assert!(peer(&socket).is_err());
    }
}
