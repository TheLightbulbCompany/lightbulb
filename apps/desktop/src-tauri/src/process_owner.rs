//! One desktop service owner per macOS user, including during app updates.
//!
//! Tauri's macOS single-instance socket is useful for forwarding launches, but
//! its connect/unlink/bind startup sequence is not an exclusive lock. Two
//! simultaneous launches can both reach app setup and mirror the same sessions.

#[cfg(target_os = "macos")]
mod macos {
    use std::{
        fs::{File, OpenOptions},
        io::{self, Write},
        os::{
            fd::AsRawFd,
            unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt},
            unix::net::UnixStream,
        },
        path::{Path, PathBuf},
        sync::OnceLock,
        time::{Duration, Instant},
    };

    static OWNER: OnceLock<File> = OnceLock::new();
    static RESTART: OnceLock<File> = OnceLock::new();
    const HANDOFF_TIMEOUT: Duration = Duration::from_secs(10);

    pub enum Claim {
        Primary,
        Forwarded,
    }

    fn lock_file(path: &Path) -> io::Result<File> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(path)?;
        let metadata = file.metadata()?;
        if !metadata.is_file()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o022 != 0
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "unsafe desktop lock",
            ));
        }
        Ok(file)
    }

    fn try_lock(file: &File) -> io::Result<bool> {
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            return Ok(true);
        }
        let error = io::Error::last_os_error();
        if matches!(error.raw_os_error(), Some(libc::EWOULDBLOCK)) {
            Ok(false)
        } else {
            Err(error)
        }
    }

    fn unlock(file: &File) -> io::Result<()> {
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) } == 0 {
            Ok(())
        } else {
            Err(io::Error::last_os_error())
        }
    }

    fn base(identifier: &str) -> String {
        identifier.replace(['.', '-'], "_")
    }

    fn path(identifier: &str, suffix: &str) -> PathBuf {
        PathBuf::from(format!(
            "/tmp/{}_{}_{}.lock",
            base(identifier),
            unsafe { libc::geteuid() },
            suffix
        ))
    }

    fn socket_path(identifier: &str) -> PathBuf {
        // tauri-plugin-single-instance 2.4.0's macOS path (semver feature disabled).
        PathBuf::from(format!("/tmp/{}_si.sock", base(identifier)))
    }

    fn forward(socket: &Path) -> io::Result<()> {
        let metadata = std::fs::symlink_metadata(socket)?;
        if !metadata.file_type().is_socket() || metadata.uid() != unsafe { libc::geteuid() } {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "unsafe desktop launch socket",
            ));
        }
        let mut stream = UnixStream::connect(socket)?;
        let cwd = std::env::current_dir().unwrap_or_default();
        stream.write_all(cwd.to_str().unwrap_or_default().as_bytes())?;
        stream.write_all(b"\0\0")?;
        stream.write_all(std::env::args().collect::<Vec<_>>().join("\0").as_bytes())?;
        stream.flush()
    }

    fn wait_for_old_socket(socket: &Path, started: Instant) -> io::Result<()> {
        // The first guarded build can be spawned before its unguarded updater
        // exits. Do not let Tauri forward that replacement back to the old app.
        match UnixStream::connect(socket) {
            Ok(_) => {}
            Err(e)
                if matches!(
                    e.kind(),
                    io::ErrorKind::NotFound | io::ErrorKind::ConnectionRefused
                ) =>
            {
                return Ok(())
            }
            Err(e) => return Err(e),
        }
        // The plugin removes this socket on Exit. One EOF probe is enough;
        // repeatedly connecting would keep focusing the old app.
        while socket.exists() {
            if started.elapsed() >= HANDOFF_TIMEOUT {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "previous desktop socket is still active",
                ));
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        Ok(())
    }

    /// Called before Tauri initializes plugins or starts the node and relay.
    pub fn claim(identifier: &str) -> io::Result<Claim> {
        let owner = lock_file(&path(identifier, "owner"))?;
        let restart = lock_file(&path(identifier, "restart"))?;
        let started = Instant::now();
        loop {
            if try_lock(&owner)? {
                wait_for_old_socket(&socket_path(identifier), started)?;
                OWNER.set(owner).map_err(|_| {
                    io::Error::new(io::ErrorKind::AlreadyExists, "desktop owner already set")
                })?;
                return Ok(Claim::Primary);
            }
            // The updater's child must become the new primary, not forward its
            // plain argv to a process that is already exiting.
            if try_lock(&restart)? {
                unlock(&restart)?;
                if forward(&socket_path(identifier)).is_ok() {
                    return Ok(Claim::Forwarded);
                }
            }
            if started.elapsed() >= HANDOFF_TIMEOUT {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "desktop owner did not accept launch",
                ));
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// Hold a second OS lock until the updater's old process has exited.
    pub fn mark_restarting(identifier: &str) -> io::Result<()> {
        if OWNER.get().is_none() {
            return Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "desktop owner unavailable",
            ));
        }
        let restart = lock_file(&path(identifier, "restart"))?;
        if !try_lock(&restart)? {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "desktop restart already in progress",
            ));
        }
        RESTART.set(restart).map_err(|_| {
            io::Error::new(io::ErrorKind::AlreadyExists, "desktop restart already set")
        })
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::{io::Read, os::unix::net::UnixListener};

        #[test]
        fn lock_allows_one_owner_and_releases_on_drop() {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("owner.lock");
            let first = lock_file(&path).unwrap();
            let second = lock_file(&path).unwrap();
            assert!(try_lock(&first).unwrap());
            assert!(!try_lock(&second).unwrap());
            drop(first);
            assert!(try_lock(&second).unwrap());
        }

        #[test]
        fn simultaneous_launches_choose_one_owner() {
            use std::sync::{Arc, Barrier};
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("owner.lock");
            let start = Arc::new(Barrier::new(2));
            let checked = Arc::new(Barrier::new(2));
            let handles: Vec<_> = (0..2)
                .map(|_| {
                    let path = path.clone();
                    let start = start.clone();
                    let checked = checked.clone();
                    std::thread::spawn(move || {
                        let file = lock_file(&path).unwrap();
                        start.wait();
                        let owner = try_lock(&file).unwrap();
                        checked.wait(); // Keep the winner's lock until both have tried.
                        owner
                    })
                })
                .collect();
            assert_eq!(
                handles
                    .into_iter()
                    .map(|h| h.join().unwrap())
                    .filter(|v| *v)
                    .count(),
                1
            );
        }

        #[test]
        fn forwards_the_plugin_wire_format() {
            let dir = tempfile::tempdir().unwrap();
            let socket = dir.path().join("single.sock");
            let listener = UnixListener::bind(&socket).unwrap();
            forward(&socket).unwrap();
            let (mut stream, _) = listener.accept().unwrap();
            let mut payload = String::new();
            stream.read_to_string(&mut payload).unwrap();
            let (_, args) = payload.split_once("\0\0").unwrap();
            assert_eq!(args.split('\0').next(), std::env::args().next().as_deref());
        }

        #[test]
        fn old_socket_probe_closes_without_payload() {
            let dir = tempfile::tempdir().unwrap();
            let socket = dir.path().join("old.sock");
            let listener = UnixListener::bind(&socket).unwrap();
            let old_socket = socket.clone();
            let old = std::thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let mut payload = Vec::new();
                stream.read_to_end(&mut payload).unwrap();
                assert!(payload.is_empty());
                drop(listener);
                std::fs::remove_file(old_socket).unwrap();
            });
            wait_for_old_socket(&socket, Instant::now()).unwrap();
            old.join().unwrap();
        }
    }
}

#[cfg(target_os = "macos")]
pub use macos::{claim, mark_restarting, Claim};

#[cfg(not(target_os = "macos"))]
pub enum Claim {
    Primary,
    Forwarded,
}

#[cfg(not(target_os = "macos"))]
pub fn claim(_identifier: &str) -> std::io::Result<Claim> {
    Ok(Claim::Primary)
}

#[cfg(not(target_os = "macos"))]
pub fn mark_restarting(_identifier: &str) -> std::io::Result<()> {
    Ok(())
}
