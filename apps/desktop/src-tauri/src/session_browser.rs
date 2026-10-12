//! The Lightbulb browser for shared Claude Code and Codex sessions (spec 2026-10-11).

/// What a session's CDP client reads when the Lightbulb browser refuses it.
pub(crate) const NOT_SHARED: &str = "Share this session to use the Lightbulb browser";

/// `struct socket_fdinfo` (<sys/proc_info.h>): libc has the call but not the struct. These offsets
/// are pinned by `peer_pid_finds_this_processs_own_client`.
const SOCKET_FDINFO_SIZE: usize = 792;
const PROC_PIDFDSOCKETINFO: i32 = 3;
const SOCKINFO_TCP: i32 = 2;
const SOI_KIND: usize = 256;
const TCP_FPORT: usize = 264;
const TCP_LPORT: usize = 268;

/// The pid that owns the client end of a loopback TCP connection to `server_port` from
/// `client_port` (spike 2026-10-11: libproc socket matching, ~0.3 ms median, no root). Looks at
/// this user's processes only; None when no process does (deny).
#[cfg(target_os = "macos")]
pub(crate) fn peer_pid(server_port: u16, client_port: u16) -> Option<u32> {
    let n = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
    let mut pids = vec![0 as libc::pid_t; n.max(0) as usize + 64];
    let bytes = (pids.len() * std::mem::size_of::<libc::pid_t>()) as i32;
    let n = unsafe { libc::proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
    pids.truncate(n.max(0) as usize);
    pids.into_iter()
        .filter(|pid| *pid > 1)
        .find(|&pid| crate::holder::identity::read(pid as u32).is_some() && owns_client(pid, server_port, client_port))
        .map(|pid| pid as u32)
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn peer_pid(_server_port: u16, _client_port: u16) -> Option<u32> {
    None
}

#[cfg(target_os = "macos")]
fn owns_client(pid: libc::pid_t, server_port: u16, client_port: u16) -> bool {
    let size = std::mem::size_of::<libc::proc_fdinfo>();
    let bytes = unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, std::ptr::null_mut(), 0) };
    if bytes <= 0 {
        return false;
    }
    let mut fds = vec![unsafe { std::mem::zeroed::<libc::proc_fdinfo>() }; bytes as usize / size + 16];
    let bytes = unsafe { libc::proc_pidinfo(pid, libc::PROC_PIDLISTFDS, 0, fds.as_mut_ptr().cast(), (fds.len() * size) as i32) };
    fds.truncate(bytes.max(0) as usize / size);
    fds.iter().filter(|fd| fd.proc_fdtype == libc::PROX_FDTYPE_SOCKET as u32).any(|fd| {
        let mut info = [0u8; SOCKET_FDINFO_SIZE];
        let got = unsafe {
            libc::proc_pidfdinfo(pid, fd.proc_fd, PROC_PIDFDSOCKETINFO, info.as_mut_ptr().cast(), SOCKET_FDINFO_SIZE as i32)
        };
        let int = |at: usize| i32::from_ne_bytes(info[at..at + 4].try_into().unwrap_or([0; 4]));
        // insi_lport/insi_fport hold the port in network byte order in their low 16 bits.
        got == SOCKET_FDINFO_SIZE as i32
            && int(SOI_KIND) == SOCKINFO_TCP
            && u16::from_be(int(TCP_LPORT) as u16) == client_port
            && u16::from_be(int(TCP_FPORT) as u16) == server_port
    })
}

/// `pid` and its parents, closest first, this user's processes only, at most 32: the session's own
/// process is somewhere up there (npx -> node -> claude).
pub(crate) fn ancestors(pid: u32) -> Vec<u32> {
    let mut chain = Vec::new();
    let mut next = Some(pid);
    while let Some(pid) = next.filter(|p| *p > 1 && chain.len() < 32) {
        chain.push(pid);
        next = crate::holder::identity::read(pid).map(|identity| identity.parent);
    }
    chain
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "macos")]
    #[test]
    fn peer_pid_finds_this_processs_own_client() {
        // Pins the socket_fdinfo offsets on a real loopback socket: this process is both ends.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let server = listener.local_addr().unwrap().port();
        let client = std::net::TcpStream::connect(("127.0.0.1", server)).unwrap();
        let _accepted = listener.accept().unwrap();
        let port = client.local_addr().unwrap().port();
        assert_eq!(peer_pid(server, port), Some(std::process::id()));
        assert_eq!(peer_pid(server, 1), None, "nobody's client port");
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn ancestors_walk_up_from_a_child_to_this_process() {
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let chain = ancestors(child.id());
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(chain[0], child.id());
        assert_eq!(chain[1], std::process::id());
        assert!(chain.len() <= 32 && !chain.contains(&1));
    }
}
