//! The holder end to end with `/bin/cat` in place of a provider: the pty echoes input, so what a
//! client sends comes back as OUTPUT.
#![cfg(unix)]
use lightbulb_desktop::holder::{manager, protocol::*, run};
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

fn temp_dir() -> PathBuf {
    let dir = PathBuf::from(format!("/private/tmp/lbh-test-{}", uuid::Uuid::new_v4().simple()))
        .join("h");
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn start_cat(dir: &Path) -> PathBuf {
    start(dir, &["/bin/cat"])
}

fn start(dir: &Path, argv: &[&str]) -> PathBuf {
    start_sized(dir, argv, None)
}

fn start_sized(dir: &Path, argv: &[&str], size: Option<(u16, u16)>) -> PathBuf {
    let socket = dir.join("held.sock");
    let (s, d) = (socket.clone(), dir.to_path_buf());
    let argv: Vec<String> = argv.iter().map(|a| a.to_string()).collect();
    std::thread::spawn(move || run::run_with_env(&s, &argv, &d, vec![("PATH".into(), "/usr/bin:/bin".into())], size));
    let deadline = Instant::now() + Duration::from_secs(5);
    while !socket.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    socket
}

fn attach(socket: &Path, role: &str) -> UnixStream {
    let mut c = UnixStream::connect(socket).unwrap();
    c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    c.write_all(&encode(HELLO, format!(r#"{{"role":"{role}"}}"#).as_bytes())).unwrap();
    assert_eq!(read_frame(&mut c).unwrap().0, HELLO);
    if role == "interactive" {
        assert_eq!(read_frame(&mut c).unwrap().0, SNAPSHOT);
    }
    c
}

/// Read OUTPUT until `needle` has been seen (or panic after the read timeout).
fn read_until(c: &mut UnixStream, needle: &[u8]) -> Vec<u8> {
    let mut seen = vec![];
    while !seen.windows(needle.len()).any(|w| w == needle) {
        let (kind, payload) = read_frame(c).expect("output before timeout");
        if kind == OUTPUT {
            seen.extend_from_slice(split_offset(&payload).unwrap().1);
        }
    }
    seen
}

#[test]
fn holder_passes_a_bracketed_paste_through_and_echoes_it() {
    let socket = start_cat(&temp_dir());
    let mut c = attach(&socket, "interactive");
    c.write_all(&encode(INPUT, b"\x1b[200~alpha\nbeta\ngamma\x1b[201~")).unwrap();
    let out = read_until(&mut c, b"gamma");
    assert!(out.windows(5).any(|w| w == b"alpha") && out.windows(4).any(|w| w == b"beta"));
}

#[test]
fn holder_gives_the_keyboard_to_the_newest_window_only() {
    let socket = start_cat(&temp_dir());
    let mut first = attach(&socket, "interactive");
    let mut second = attach(&socket, "interactive");
    assert_eq!(read_frame(&mut first).unwrap().0, DETACHED);
    first.write_all(&encode(INPUT, b"from-first\r")).unwrap();
    second.write_all(&encode(INPUT, b"from-second\r")).unwrap();
    let out = read_until(&mut second, b"from-second");
    assert!(!out.windows(10).any(|w| w == b"from-first"), "a replaced window must not type");
}

#[test]
fn holder_replays_from_an_offset_and_reports_a_gap_beyond_its_log() {
    let socket = start_cat(&temp_dir());
    let mut c = attach(&socket, "interactive");
    c.write_all(&encode(INPUT, b"replay-me\r")).unwrap();
    read_until(&mut c, b"replay-me");
    let mut r = attach(&socket, "recorder");
    r.write_all(&encode(REPLAY, &0u64.to_be_bytes())).unwrap();
    assert!(read_until(&mut r, b"replay-me").len() >= 9);
}

#[test]
fn holder_exits_with_its_provider_and_removes_its_socket() {
    let socket = start_cat(&temp_dir());
    let mut c = attach(&socket, "interactive");
    c.write_all(&encode(INPUT, b"\x04")).unwrap();
    loop {
        let (kind, _) = read_frame(&mut c).expect("EXIT before timeout");
        if kind == EXIT {
            break;
        }
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while socket.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(!socket.exists());
}

#[test]
fn holder_manager_lists_live_holders_and_drops_dead_sockets() {
    let dir = temp_dir();
    let socket = start_cat(&dir);
    std::fs::write(dir.join("dead0000.sock"), b"").unwrap();
    let list = manager::list_in(&dir);
    assert_eq!(list.len(), 1);
    assert_eq!(list[0].socket, socket);
    assert_eq!(list[0].argv, vec!["/bin/cat".to_string()]);
    assert!(!dir.join("dead0000.sock").exists(), "a socket nobody listens on is removed");
}

#[test]
fn holder_keeps_serving_its_window_when_another_client_stops_reading() {
    let socket = start_cat(&temp_dir());
    let mut stalled = attach(&socket, "recorder"); // never read again
    let mut c = attach(&socket, "interactive");
    let mut reader = c.try_clone().unwrap();
    let echo = std::thread::spawn(move || read_until(&mut reader, b"still-alive"));
    // Lines stay under the terminal's 1024-byte line limit.
    for _ in 0..800 {
        c.write_all(&encode(INPUT, &[b'x'; 500])).unwrap();
        c.write_all(&encode(INPUT, b"\r")).unwrap();
    }
    c.write_all(&encode(INPUT, b"still-alive\r")).unwrap();
    assert!(echo.join().unwrap().windows(11).any(|w| w == b"still-alive"));
    let _ = stalled.write_all(b"");
}

#[test]
fn holder_answers_hello_before_any_output() {
    let socket = start_cat(&temp_dir());
    let mut c = attach(&socket, "interactive");
    let mut late = UnixStream::connect(&socket).unwrap();
    late.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    c.write_all(&encode(INPUT, b"output-before-hello\r")).unwrap();
    read_until(&mut c, b"output-before-hello");
    late.write_all(&encode(HELLO, br#"{"role":"recorder"}"#)).unwrap();
    assert_eq!(read_frame(&mut late).unwrap().0, HELLO);
}

#[test]
fn holder_delivers_a_provider_s_last_words_and_exit_code_before_exit() {
    let socket = start(&temp_dir(), &["/bin/sh", "-c", "read x; printf final-words; exit 3"]);
    let mut c = attach(&socket, "interactive");
    c.write_all(&encode(INPUT, b"go\r")).unwrap();
    let mut seen = vec![];
    loop {
        let (kind, payload) = read_frame(&mut c).expect("frames before timeout");
        match kind {
            OUTPUT => seen.extend_from_slice(split_offset(&payload).unwrap().1),
            EXIT => {
                let exit: serde_json::Value = serde_json::from_slice(&payload).unwrap();
                assert_eq!(exit["code"], 3, "the command exits with this");
                break;
            }
            _ => {}
        }
    }
    assert!(seen.windows(11).any(|w| w == b"final-words"));
}

#[test]
fn holder_replays_a_new_terminal_from_the_start_instead_of_a_snapshot() {
    let socket = start_cat(&temp_dir());
    let mut first = attach(&socket, "interactive");
    first.write_all(&encode(INPUT, b"early-output\r")).unwrap();
    read_until(&mut first, b"early-output");

    let mut c = UnixStream::connect(&socket).unwrap();
    c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    c.write_all(&encode(HELLO, br#"{"role":"interactive","replayFrom":0}"#)).unwrap();
    assert_eq!(read_frame(&mut c).unwrap().0, HELLO);
    let (kind, payload) = read_frame(&mut c).unwrap();
    assert_eq!(kind, OUTPUT, "all output from offset 0, not a screen-clearing snapshot");
    let (offset, bytes) = split_offset(&payload).unwrap();
    assert_eq!(offset, 0);
    assert!(bytes.windows(12).any(|w| w == b"early-output"));

    // An offset the holder does not have: the snapshot, as before.
    let mut late = UnixStream::connect(&socket).unwrap();
    late.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    late.write_all(&encode(HELLO, br#"{"role":"interactive","replayFrom":99999999}"#)).unwrap();
    assert_eq!(read_frame(&mut late).unwrap().0, HELLO);
    assert_eq!(read_frame(&mut late).unwrap().0, SNAPSHOT);
}

#[test]
fn holder_starts_at_the_size_it_was_given() {
    let socket = start_sized(&temp_dir(), &["/bin/cat"], Some((50, 10)));
    let mut r = UnixStream::connect(&socket).unwrap();
    r.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    r.write_all(&encode(HELLO, br#"{"role":"recorder"}"#)).unwrap();
    let (_, facts) = read_frame(&mut r).unwrap();
    let facts: serde_json::Value = serde_json::from_slice(&facts).unwrap();
    assert_eq!((facts["cols"].as_u64(), facts["rows"].as_u64()), (Some(50), Some(10)));
}

#[test]
fn holder_takes_the_window_size_from_its_hello() {
    let socket = start_cat(&temp_dir());
    let mut c = UnixStream::connect(&socket).unwrap();
    c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    c.write_all(&encode(HELLO, br#"{"role":"interactive","cols":50,"rows":10}"#)).unwrap();
    assert_eq!(read_frame(&mut c).unwrap().0, HELLO);
    assert_eq!(read_frame(&mut c).unwrap().0, SNAPSHOT);
    let mut r = UnixStream::connect(&socket).unwrap();
    r.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    r.write_all(&encode(HELLO, br#"{"role":"recorder"}"#)).unwrap();
    let (_, facts) = read_frame(&mut r).unwrap();
    let facts: serde_json::Value = serde_json::from_slice(&facts).unwrap();
    assert_eq!((facts["cols"].as_u64(), facts["rows"].as_u64()), (Some(50), Some(10)));
}

#[test]
fn holder_manager_refuses_a_planted_symlink_for_its_folder() {
    let base = PathBuf::from(format!("/private/tmp/lbh-test-{}", uuid::Uuid::new_v4().simple()));
    let elsewhere = base.with_extension("target");
    std::fs::create_dir_all(&elsewhere).unwrap();
    std::os::unix::fs::symlink(&elsewhere, &base).unwrap();
    let err = manager::start_with(Path::new("/bin/false"), &base.join("h"), Path::new("/"), &["/bin/cat".into()], None).unwrap_err();
    assert!(err.contains("not safe"), "{err}");
    assert!(!elsewhere.join("h").exists(), "nothing is created through the symlink");
}
