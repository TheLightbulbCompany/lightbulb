//! The Lightbulb browser for shared Claude Code and Codex sessions (spec 2026-10-11).

/// What a session's CDP client reads when the Lightbulb browser refuses it.
pub(crate) const NOT_SHARED: &str = "Share this session to use the Lightbulb browser";

use crate::holder::identity::{self, Identity};
use std::net::SocketAddr;

/// `struct socket_fdinfo` (<sys/proc_info.h>): libc has the call but not the struct. These offsets
/// are pinned by `peer_pid_finds_this_processs_own_client` (IPv4 and IPv6).
const SOCKET_FDINFO_SIZE: usize = 792;
const PROC_PIDFDSOCKETINFO: i32 = 3;
const SOCKINFO_TCP: i32 = 2;
const SOI_KIND: usize = 256;
const TCP_FPORT: usize = 264;
const TCP_LPORT: usize = 268;
const INSI_VFLAG: usize = 288;
const INSI_FADDR: usize = 296;
const INSI_LADDR: usize = 312;
const INI_IPV4: u8 = 0x1;

/// The process that owns the client end of a loopback TCP connection: its local end is `client`
/// (the accepted stream's peer) and its foreign end `server` (the accepted stream's local
/// address). Spike 2026-10-11: libproc socket matching, ~0.3 ms median, no root. Looks at this
/// user's processes only; None when no process or more than one does (deny).
#[cfg(target_os = "macos")]
pub(crate) fn peer_pid(server: SocketAddr, client: SocketAddr) -> Option<Identity> {
    let n = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
    let mut pids = vec![0 as libc::pid_t; n.max(0) as usize + 64];
    let bytes = (pids.len() * std::mem::size_of::<libc::pid_t>()) as i32;
    let n = unsafe { libc::proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
    pids.truncate(n.max(0) as usize);
    let (server, client) = (canonical(server), canonical(client));
    sole_owner(
        pids.into_iter()
            .filter(|pid| *pid > 1)
            .filter_map(|pid| identity::read(pid as u32).filter(|_| owns_client(pid, server, client))),
    )
}

#[cfg(not(target_os = "macos"))]
pub(crate) fn peer_pid(_server: SocketAddr, _client: SocketAddr) -> Option<Identity> {
    None
}

/// One process, or nobody: a socket two processes hold (a fork-inherited fd) names no one.
fn sole_owner<T: PartialEq>(owners: impl IntoIterator<Item = T>) -> Option<T> {
    let mut owners = owners.into_iter();
    let first = owners.next()?;
    owners.all(|other| other == first).then_some(first)
}

fn canonical(addr: SocketAddr) -> SocketAddr {
    SocketAddr::new(addr.ip().to_canonical(), addr.port())
}

#[cfg(target_os = "macos")]
fn owns_client(pid: libc::pid_t, server: SocketAddr, client: SocketAddr) -> bool {
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
        got == SOCKET_FDINFO_SIZE as i32 && tcp_ends(&info) == Some((client, server))
    })
}

/// A TCP socket's (local, foreign) ends, canonical (a v4-mapped IPv6 address reads as IPv4).
fn tcp_ends(info: &[u8; SOCKET_FDINFO_SIZE]) -> Option<(SocketAddr, SocketAddr)> {
    let int = |at: usize| i32::from_ne_bytes(info[at..at + 4].try_into().unwrap_or([0; 4]));
    if int(SOI_KIND) != SOCKINFO_TCP {
        return None;
    }
    let end = |addr_at: usize, port_at: usize| {
        let bytes: [u8; 16] = info[addr_at..addr_at + 16].try_into().unwrap_or([0; 16]);
        // An IPv4 end keeps its address in the last 4 bytes (in4in6_addr).
        let ip = if info[INSI_VFLAG] & INI_IPV4 != 0 {
            std::net::IpAddr::from([bytes[12], bytes[13], bytes[14], bytes[15]])
        } else {
            std::net::IpAddr::from(bytes)
        };
        // insi_lport/insi_fport hold the port in network byte order in their low 16 bits.
        canonical(SocketAddr::new(ip, u16::from_be(int(port_at) as u16)))
    };
    Some((end(INSI_LADDR, TCP_LPORT), end(INSI_FADDR, TCP_FPORT)))
}

/// The peer's pid and its parents, closest first, this user's processes only, at most 32: the
/// session's own process is somewhere up there (npx -> node -> claude). Empty (deny) when the
/// peer is no longer the process the socket lookup named, before or after the walk.
pub(crate) fn ancestors(peer: Identity) -> Vec<u32> {
    if identity::read(peer.pid) != Some(peer) {
        return Vec::new();
    }
    let mut chain = Vec::new();
    let mut next = Some(peer.pid);
    while let Some(pid) = next.filter(|p| *p > 1 && chain.len() < 32) {
        chain.push(pid);
        next = identity::read(pid).map(|identity| identity.parent);
    }
    if identity::read(peer.pid) != Some(peer) {
        return Vec::new();
    }
    chain
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(target_os = "macos")]
    #[test]
    fn peer_pid_finds_this_processs_own_client() {
        // Pins the socket_fdinfo offsets on real loopback sockets: this process is both ends.
        for loopback in ["127.0.0.1:0", "[::1]:0"] {
            let listener = std::net::TcpListener::bind(loopback).unwrap();
            let client = std::net::TcpStream::connect(listener.local_addr().unwrap()).unwrap();
            let (accepted, _) = listener.accept().unwrap();
            let (server, peer) = (accepted.local_addr().unwrap(), accepted.peer_addr().unwrap());
            assert_eq!(peer, client.local_addr().unwrap());
            let found = peer_pid(server, peer).map(|identity| identity.pid);
            assert_eq!(found, Some(std::process::id()), "{loopback}");
            let nobody = SocketAddr::new(peer.ip(), 1);
            assert_eq!(peer_pid(server, nobody), None, "nobody's client port");
        }
    }

    #[test]
    fn a_socket_more_than_one_process_holds_names_no_one() {
        assert_eq!(sole_owner([7, 7]), Some(7), "one process, the fd twice");
        assert_eq!(sole_owner([7, 8]), None, "a fork-inherited fd: parent and child");
        assert_eq!(sole_owner(Vec::<u32>::new()), None);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn ancestors_walk_up_from_a_child_to_this_process() {
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let peer = identity::read(child.id()).unwrap();
        let chain = ancestors(peer);
        let stale = ancestors(Identity { birth_micros: peer.birth_micros + 1, ..peer });
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(chain[0], child.id());
        assert_eq!(chain[1], std::process::id());
        assert!(chain.len() <= 32 && !chain.contains(&1));
        assert!(stale.is_empty(), "a reused pid is not the peer the socket named");
    }
}
