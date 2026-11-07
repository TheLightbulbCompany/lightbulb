//! The holder end to end with `/bin/cat` in place of a provider: the pty echoes input, so what a
//! client sends comes back as OUTPUT.
// macOS until the Linux port (plan PR 8): the sockets live under /private/tmp.
#![cfg(target_os = "macos")]
use lightbulb_core::holder::{manager, protocol::*, run};
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

/// An attach holder is one more client of the provider's own host (spec 2026-10-07 §4 k): when
/// that client leaves there is no screen of ours to keep, and the next viewer needs a new client.
#[test]
fn holder_attach_client_exit_closes_the_holder_and_removes_its_socket() {
    let dir = temp_dir();
    let argv = manager::attach_argv(Path::new("/bin/echo"), "8ed71738");
    let argv: Vec<&str> = argv.iter().map(String::as_str).collect();
    let socket = start(&dir, &argv);
    let deadline = Instant::now() + Duration::from_secs(5);
    while socket.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(!socket.exists(), "an attach holder ends with its client");
    assert!(manager::list_in(&dir).is_empty());
}

#[test]
fn holder_keeps_the_final_screen_after_its_provider_exits_and_refuses_input() {
    let dir = temp_dir();
    let socket = start(&dir, &["/bin/sh", "-c", "read x; printf final-screen; exit 0"]);
    std::fs::write(socket.with_extension("thread"), "11111111-1111-4111-8111-111111111111").unwrap();
    assert_eq!(manager::ended_at(&socket), None, "a running holder is not ended");
    let mut c = attach(&socket, "interactive");
    c.write_all(&encode(INPUT, b"go\r")).unwrap();
    loop {
        let (kind, _) = read_frame(&mut c).expect("EXIT before timeout");
        if kind == EXIT {
            break;
        }
    }
    assert_eq!(last_input(&socket), "owner");
    // Still listed, and it says it ended.
    let info = manager::list_in(&dir).into_iter().next().expect("the holder stays after its provider");
    assert!(manager::is_ended(&info));
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis() as u64;
    let at = manager::ended_at(&socket).expect("an ended holder says when its provider exited");
    assert!(now.saturating_sub(at) < 30_000, "{at} is not a recent unix time in ms");
    assert!(!socket.with_extension("thread").exists(), "an ended holder never claims a Codex thread");
    // A new viewer gets the final screen.
    let mut w = UnixStream::connect(&socket).unwrap();
    w.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    w.write_all(&encode(HELLO, br#"{"role":"watch"}"#)).unwrap();
    let (kind, hello) = read_frame(&mut w).unwrap();
    assert_eq!(kind, HELLO);
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&hello).unwrap()["ended"], true);
    let (kind, snap) = read_frame(&mut w).unwrap();
    assert_eq!(kind, SNAPSHOT);
    assert!(split_offset(&snap).unwrap().1.windows(12).any(|x| x == b"final-screen"));
    // An interactive client is only a watcher now, and a teammate's keys go nowhere.
    let mut late = UnixStream::connect(&socket).unwrap();
    late.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    late.write_all(&encode(HELLO, br#"{"role":"interactive","cols":10,"rows":5}"#)).unwrap();
    assert_eq!(read_frame(&mut late).unwrap().0, HELLO);
    assert_eq!(read_frame(&mut late).unwrap().0, SNAPSHOT);
    assert_eq!(size_of(&socket), (120, 40), "a late window never resizes the final screen");
    let mut t = UnixStream::connect(&socket).unwrap();
    t.write_all(&encode(HELLO, br#"{"role":"type"}"#)).unwrap();
    t.write_all(&encode(INPUT, b"typed-after-exit")).unwrap();
    std::thread::sleep(Duration::from_millis(200));
    assert_eq!(last_input(&socket), "owner", "input after the end is refused");
    // ponytail: the holder thread outlives this test (in-process); `manager::stop` would signal the test runner.
}

/// Prod 2026-10-08: a process closes its terminal before it can be waited for, and the holder then
/// waited a whole second more for output that could not come. Every viewer read the provider's
/// way out (Claude Code's slash menu and resume lines) that much longer before the kept picture.
#[test]
fn holder_says_exit_once_the_terminal_closed_and_the_provider_is_reaped() {
    let socket = start(&temp_dir(), &["/bin/sh", "-c", "read x; printf last-words; exit 0"]);
    let mut c = attach(&socket, "interactive");
    let sent = Instant::now();
    run_to_exit(&mut c);
    assert!(sent.elapsed() < Duration::from_millis(700), "EXIT came {:?} after the provider was told to go", sent.elapsed());
}

/// Send `go` to a provider that waits on `read`, then follow the holder to its EXIT.
fn run_to_exit(c: &mut UnixStream) {
    c.write_all(&encode(INPUT, b"go\r")).unwrap();
    while read_frame(c).expect("EXIT before timeout").0 != EXIT {}
}

/// What an ended holder shows a new watch client, and again when that client asks for a snapshot.
fn ended_screen(socket: &Path) -> Vec<u8> {
    let mut w = attach(socket, "watch");
    let (kind, first) = read_frame(&mut w).unwrap();
    assert_eq!(kind, SNAPSHOT);
    w.write_all(&encode(SNAPSHOT, b"")).unwrap();
    let (kind, again) = read_frame(&mut w).unwrap();
    assert_eq!(kind, SNAPSHOT);
    assert_eq!(split_offset(&first).unwrap().1, split_offset(&again).unwrap().1, "every snapshot of an ended holder is the same picture");
    split_offset(&first).unwrap().1.to_vec()
}

fn has(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

/// Claude Code's fullscreen renderer on `/exit`: it leaves the alternate screen, prints two lines
/// to the normal one and exits. The kept screen is the last picture the person saw (prod 0.4.625
/// kept only the two lines).
#[test]
fn holder_keeps_the_last_alternate_picture_when_the_provider_leaves_it_to_exit() {
    let dir = temp_dir();
    let socket = start(&dir, &["/bin/sh", "-c", concat!(
        r"read x; printf '\033[?1049h\033[2J\033[H\033[?1000h\033[1mconversation-top\033[0m\r\nlast-answer\r\n> prompt-box';",
        r" printf '\033[?1000l\033[?1049l'; printf 'Resume this session with:\r\nclaude --resume 8ed71738\r\n'; exit 0")]);
    let mut c = attach(&socket, "interactive");
    run_to_exit(&mut c);
    let screen = ended_screen(&socket);
    for line in [&b"conversation-top"[..], b"last-answer", b"> prompt-box"] {
        assert!(has(&screen, line), "the kept screen has {:?}: {:?}", String::from_utf8_lossy(line), String::from_utf8_lossy(&screen));
    }
    assert!(!has(&screen, b"claude --resume"), "not the shell's exit lines");
    assert!(!has(&screen, b"\x1b[?1049h"), "served as normal-buffer content, so the viewer can scroll and select");
    assert!(!has(&screen, b"\x1b[?1000h"), "a read-only picture turns no mouse reporting on");
}

/// A full-screen program (vim, less) that closed long before the session ended is not the last
/// picture: the normal screen is.
#[test]
fn holder_keeps_the_normal_screen_when_the_alternate_one_closed_long_before_the_exit() {
    let dir = temp_dir();
    let socket = start(&dir, &["/bin/sh", "-c", concat!(
        r"read x; printf '\033[?1049h\033[2J\033[Hvim-buffer\033[?1049l'; printf 'after-vim\r\n';",
        r" read y; printf 'final-words'; exit 0")]);
    let mut c = attach(&socket, "interactive");
    c.write_all(&encode(INPUT, b"go\r")).unwrap();
    read_until(&mut c, b"after-vim");
    std::thread::sleep(Duration::from_millis(5300)); // past the 5 s in which a leave counts as shutting down
    run_to_exit(&mut c);
    let screen = ended_screen(&socket);
    assert!(has(&screen, b"after-vim") && has(&screen, b"final-words"), "{:?}", String::from_utf8_lossy(&screen));
    assert!(!has(&screen, b"vim-buffer"));
}

#[test]
fn holder_manager_lists_live_holders_and_ignores_dead_sockets() {
    let dir = temp_dir();
    let socket = start_cat(&dir);
    // A regular file is not a refused endpoint on every OS. Leave a real socket
    // pathname behind after its listener closes, as a dead holder does.
    let dead = std::os::unix::net::UnixListener::bind(dir.join("dead0000.sock")).unwrap();
    drop(dead);
    let list = manager::list_in(&dir);
    assert_eq!(list.len(), 1);
    assert_eq!(list[0].socket, socket);
    assert_eq!(list[0].argv, vec!["/bin/cat".to_string()]);
    assert!(dir.join("dead0000.sock").exists(), "discovery never unlinks an endpoint based on a failed connection");
}

/// An idle stop may end a holder only while no terminal or panel has its keyboard; a watcher
/// (the app's live-view relay) does not count as one.
#[test]
fn holder_says_whether_a_terminal_is_attached() {
    let dir = temp_dir();
    start_cat(&dir);
    let unattached = || manager::unattached(&manager::list_in(&dir)).len();
    assert_eq!(unattached(), 1, "nobody on it");
    let watch = attach(dir.join("held.sock").as_path(), "watch");
    assert_eq!(unattached(), 1, "a watcher is not a terminal");
    let window = attach(dir.join("held.sock").as_path(), "interactive");
    assert_eq!(unattached(), 0, "a terminal has the keyboard");
    drop(window);
    let deadline = Instant::now() + Duration::from_secs(5);
    while unattached() == 0 && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert_eq!(unattached(), 1, "the terminal closed");
    drop(watch);
}

/// An idle stop is refused while a terminal has the keyboard, whatever discovery saw, and taken
/// once none does: the provider then exits and the holder keeps its final screen.
#[test]
fn holder_refuses_an_idle_end_while_a_terminal_is_attached() {
    let dir = temp_dir();
    start_cat(&dir);
    let info = || manager::list_in(&dir).remove(0);
    let mut window = attach(dir.join("held.sock").as_path(), "interactive");
    assert!(!manager::end_idle(&info()), "a terminal is on it");
    window.write_all(&encode(INPUT, b"still-here\r")).unwrap();
    read_until(&mut window, b"still-here");
    drop(window);
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut taken = false;
    while !taken && Instant::now() < deadline {
        taken = manager::end_idle(&info());
        if !taken {
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    assert!(taken, "nobody on it: the holder takes the stop");
    let deadline = Instant::now() + Duration::from_secs(10);
    while manager::ended_at(&dir.join("held.sock")).is_none() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(manager::ended_at(&dir.join("held.sock")).is_some(), "its provider ended; the holder shows the final screen");
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

#[test]
fn holder_watch_client_gets_snapshots_and_sizes_and_can_never_type() {
    let socket = start_cat(&temp_dir());
    let mut w = UnixStream::connect(&socket).unwrap();
    w.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    w.write_all(&encode(HELLO, br#"{"role":"watch"}"#)).unwrap();
    let (kind, hello) = read_frame(&mut w).unwrap();
    assert_eq!(kind, HELLO);
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&hello).unwrap()["watch"], true);
    assert_eq!(read_frame(&mut w).unwrap().0, SNAPSHOT, "a watcher starts from the screen");
    assert!(manager::list_in(socket.parent().unwrap())[0].watch);

    let mut c = attach(&socket, "interactive");
    c.write_all(&encode(INPUT, b"watched\r")).unwrap();
    read_until(&mut w, b"watched");
    c.write_all(&encode(RESIZE, &[0, 90, 0, 30])).unwrap();
    loop {
        let (kind, payload) = read_frame(&mut w).unwrap();
        if kind == RESIZE {
            assert_eq!(payload, vec![0, 90, 0, 30]);
            break;
        }
    }
    w.write_all(&encode(SNAPSHOT, b"")).unwrap();
    while read_frame(&mut w).unwrap().0 != SNAPSHOT {}

    w.write_all(&encode(INPUT, b"from-watcher\r")).unwrap();
    w.write_all(&encode(RESIZE, &[0, 20, 0, 5])).unwrap();
    c.write_all(&encode(INPUT, b"from-owner\r")).unwrap();
    let out = read_until(&mut c, b"from-owner");
    assert!(!out.windows(12).any(|x| x == b"from-watcher"), "a watcher never types");
}

#[test]
fn holder_type_client_types_but_never_resizes_and_gets_no_output() {
    let socket = start_cat(&temp_dir());
    let mut w = attach(&socket, "watch");
    assert_eq!(read_frame(&mut w).unwrap().0, SNAPSHOT);
    let mut t = UnixStream::connect(&socket).unwrap();
    t.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    t.write_all(&encode(HELLO, br#"{"role":"type"}"#)).unwrap();
    let (kind, hello) = read_frame(&mut t).unwrap();
    assert_eq!(kind, HELLO);
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&hello).unwrap()["type"], true);
    assert!(manager::list_in(socket.parent().unwrap())[0].type_input);

    t.write_all(&encode(INPUT, b"typed-remotely\r")).unwrap();
    read_until(&mut w, b"typed-remotely");
    t.write_all(&encode(RESIZE, &[0, 20, 0, 5])).unwrap();
    t.write_all(&encode(INPUT, b"after-resize\r")).unwrap();
    let mut seen = vec![];
    while !seen.windows(12).any(|x| x == b"after-resize") {
        let (kind, payload) = read_frame(&mut w).expect("output before timeout");
        assert_ne!(kind, RESIZE, "a type client never resizes");
        if kind == OUTPUT {
            seen.extend_from_slice(split_offset(&payload).unwrap().1);
        }
    }
    t.set_read_timeout(Some(Duration::from_millis(300))).unwrap();
    assert!(read_frame(&mut t).is_err(), "a type client receives no output");
}

/// `lastInput` as an `origin` client reads it in the holder's HELLO.
fn last_input(socket: &Path) -> serde_json::Value {
    let mut q = UnixStream::connect(socket).unwrap();
    q.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    q.write_all(&encode(HELLO, br#"{"role":"origin"}"#)).unwrap();
    let (kind, hello) = read_frame(&mut q).unwrap();
    assert_eq!(kind, HELLO);
    let facts: serde_json::Value = serde_json::from_slice(&hello).unwrap();
    assert_eq!(facts["origin"], true);
    facts["lastInput"].clone()
}

#[test]
fn holder_reports_who_wrote_the_last_input() {
    let socket = start_cat(&temp_dir());
    assert_eq!(last_input(&socket), serde_json::Value::Null, "nothing typed yet");
    let mut owner = attach(&socket, "interactive");
    let mut t = attach(&socket, "type");
    owner.write_all(&encode(INPUT, b"one\r")).unwrap();
    read_until(&mut owner, b"one");
    assert_eq!(last_input(&socket), "owner");
    t.write_all(&encode(INPUT, b"two\r")).unwrap();
    read_until(&mut owner, b"two");
    assert_eq!(last_input(&socket), "teammate", "a type client's plain INPUT is a teammate's");
    owner.write_all(&encode(INPUT, b"three\r")).unwrap();
    read_until(&mut owner, b"three");
    assert_eq!(last_input(&socket), "owner", "the owner typing again takes it back");
    t.write_all(&encode(INPUT, b"four\r")).unwrap();
    read_until(&mut owner, b"four");
    t.write_all(&encode(OWNER_INPUT, b"five\r")).unwrap();
    read_until(&mut owner, b"five");
    assert_eq!(last_input(&socket), "owner", "a type client's OWNER_INPUT is the owner's");
}

/// The size a `recorder` reads in the holder's HELLO: what the pty is now.
fn size_of(socket: &Path) -> (u64, u64) {
    let mut r = UnixStream::connect(socket).unwrap();
    r.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    r.write_all(&encode(HELLO, br#"{"role":"recorder"}"#)).unwrap();
    let (_, facts) = read_frame(&mut r).unwrap();
    let facts: serde_json::Value = serde_json::from_slice(&facts).unwrap();
    (facts["cols"].as_u64().unwrap(), facts["rows"].as_u64().unwrap())
}

#[test]
fn holder_join_client_shares_a_terminal_and_takes_it_only_when_that_terminal_leaves() {
    let socket = start_cat(&temp_dir());
    // `lightbulb claude`: the interactive client, at its terminal's size.
    let mut cli = UnixStream::connect(&socket).unwrap();
    cli.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    cli.write_all(&encode(HELLO, br#"{"role":"interactive","cols":50,"rows":10}"#)).unwrap();
    assert_eq!(read_frame(&mut cli).unwrap().0, HELLO);
    assert_eq!(read_frame(&mut cli).unwrap().0, SNAPSHOT);

    // The app's panel joins: it is told so, at the terminal's size, and starts from the screen.
    let mut panel = UnixStream::connect(&socket).unwrap();
    panel.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    panel.write_all(&encode(HELLO, br#"{"role":"interactive","join":true,"cols":90,"rows":30}"#)).unwrap();
    let (kind, hello) = read_frame(&mut panel).unwrap();
    assert_eq!(kind, HELLO);
    let facts: serde_json::Value = serde_json::from_slice(&hello).unwrap();
    assert_eq!((facts["joined"].as_bool(), facts["cols"].as_u64(), facts["rows"].as_u64()), (Some(true), Some(50), Some(10)));
    assert_eq!(read_frame(&mut panel).unwrap().0, SNAPSHOT);

    // Both type; the joiner's RESIZE is ignored; the terminal is never detached.
    cli.write_all(&encode(INPUT, b"from-cli\r")).unwrap();
    read_until(&mut panel, b"from-cli");
    panel.write_all(&encode(RESIZE, &[0, 90, 0, 30])).unwrap();
    panel.write_all(&encode(INPUT, b"from-panel\r")).unwrap();
    let mut seen = vec![];
    while !seen.windows(10).any(|w| w == b"from-panel") {
        let (kind, payload) = read_frame(&mut cli).expect("output before timeout");
        assert_ne!(kind, DETACHED, "a joining client never takes the terminal's keyboard");
        if kind == OUTPUT {
            seen.extend_from_slice(split_offset(&payload).unwrap().1);
        }
    }
    assert_eq!(size_of(&socket), (50, 10), "the terminal keeps the size");

    // The terminal leaves: the joiner is promoted, and its RESIZE now applies.
    drop(cli);
    while read_frame(&mut panel).expect("PROMOTED before the timeout").0 != PROMOTED {}
    panel.write_all(&encode(RESIZE, &[0, 90, 0, 30])).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while size_of(&socket) != (90, 30) {
        assert!(Instant::now() < deadline, "the promoted client's RESIZE applies");
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Waits for the pty to be `size`, or panics with `why`.
fn wait_size(socket: &Path, size: (u64, u64), why: &str) {
    let deadline = Instant::now() + Duration::from_secs(5);
    while size_of(socket) != size {
        assert!(Instant::now() < deadline, "{why}: {:?}", size_of(socket));
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Owner ruling 2026-10-14, tmux's `window-size latest`: the pty is the size of whichever client
/// used the session last.
#[test]
fn holder_session_takes_the_size_of_whoever_used_it_last() {
    let socket = start_cat(&temp_dir());
    let mut cli = UnixStream::connect(&socket).unwrap();
    cli.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    cli.write_all(&encode(HELLO, br#"{"role":"interactive","cols":50,"rows":10}"#)).unwrap();
    assert_eq!(read_frame(&mut cli).unwrap().0, HELLO);
    assert_eq!(read_frame(&mut cli).unwrap().0, SNAPSHOT);
    let mut t = UnixStream::connect(&socket).unwrap();
    t.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    t.write_all(&encode(HELLO, br#"{"role":"type"}"#)).unwrap();
    assert_eq!(read_frame(&mut t).unwrap().0, HELLO);
    let listed = manager::list_in(socket.parent().unwrap());
    assert!(manager::takes_resize(&listed[0].id), "this holder says it takes the panel's size");

    // The app's panel opens (a claiming RESIZE): its size applies.
    t.write_all(&encode(RESIZE, &[0, 90, 0, 30, 1])).unwrap();
    wait_size(&socket, (90, 30), "the remote panel's claim applies");
    // While it is the latest user, its plain resizes apply too.
    t.write_all(&encode(RESIZE, &[0, 80, 0, 25])).unwrap();
    wait_size(&socket, (80, 25), "the latest user's resize applies");
    // A key in the Mac's terminal takes the size back.
    cli.write_all(&encode(INPUT, b"mac\r")).unwrap();
    read_until(&mut cli, b"mac");
    wait_size(&socket, (50, 10), "the terminal's keystroke reclaims its size");
    // The panel is no longer the latest user: its plain resize is remembered, not applied, and its
    // keys alone take nothing (the app sends a claim with them).
    t.write_all(&encode(RESIZE, &[0, 70, 0, 20])).unwrap();
    t.write_all(&encode(INPUT, b"stale\r")).unwrap();
    read_until(&mut cli, b"stale");
    assert_eq!(size_of(&socket), (50, 10), "a stale client's resize is ignored");
    // The panel claims again; then the terminal's window resizes (its SIGWINCH): always a use.
    t.write_all(&encode(RESIZE, &[0, 90, 0, 30, 1])).unwrap();
    wait_size(&socket, (90, 30), "the panel claims again");
    cli.write_all(&encode(RESIZE, &[0, 60, 0, 12])).unwrap();
    wait_size(&socket, (60, 12), "the terminal's own resize reclaims");
    // The latest user leaving puts the terminal's size back.
    t.write_all(&encode(RESIZE, &[0, 90, 0, 30, 1])).unwrap();
    wait_size(&socket, (90, 30), "the panel claims once more");
    drop(t);
    wait_size(&socket, (60, 12), "the panel left: the terminal's size again");
}

#[test]
fn holder_join_client_with_nobody_attached_is_the_interactive_client() {
    let socket = start_cat(&temp_dir());
    let mut panel = UnixStream::connect(&socket).unwrap();
    panel.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    panel.write_all(&encode(HELLO, br#"{"role":"interactive","join":true,"cols":70,"rows":20}"#)).unwrap();
    let (_, hello) = read_frame(&mut panel).unwrap();
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&hello).unwrap()["joined"], false);
    assert_eq!(read_frame(&mut panel).unwrap().0, SNAPSHOT);
    assert_eq!(size_of(&socket), (70, 20));
}

#[test]
fn holder_runs_the_provider_in_the_environment_it_was_handed() {
    // The real holder binary, as `lightbulb claude` starts it: the terminal's environment through
    // the holder's own, minus another session's markers, never on its argv.
    let dir = temp_dir();
    let env = vec![("PATH".to_string(), "/usr/bin:/bin".to_string()), ("MARK".into(), "from-terminal".into()), ("CLAUDECODE".into(), "1".into()), ("CLAUDE_JOB_DIR".into(), "/tmp/jobs/other".into())];
    let info = manager::start_with_env(Path::new(env!("CARGO_BIN_EXE_lightbulb")), &dir, Path::new("/"),
        &["/bin/sh".into(), "-c".into(), "echo \"mark=$MARK claudecode=${CLAUDECODE:-unset} jobdir=${CLAUDE_JOB_DIR:-unset} inherit=${LIGHTBULB_HOLDER_INHERIT_ENV:-unset}\"; exec cat".into()], None, Some(env)).unwrap();
    let mut c = UnixStream::connect(&info.socket).unwrap();
    c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
    c.write_all(&encode(HELLO, br#"{"role":"interactive","replayFrom":0}"#)).unwrap();
    read_until(&mut c, b"mark=from-terminal claudecode=unset jobdir=unset inherit=unset");
    manager::stop(info.pid);
}

/// Owner ruling 2026-10-07: a held Claude Code session runs in Claude Code's classic renderer, so
/// its output has history a watcher can scroll back through. The real holder binary, started as
/// `lightbulb claude` starts it (the terminal's own environment, the start-link wrapper), with a
/// script under each provider's name in place of the provider.
#[test]
fn holder_starts_claude_code_in_the_classic_renderer_and_codex_as_it_is() {
    use std::os::unix::fs::PermissionsExt;
    for (name, renderer) in [("claude", "renderer=1 flicker=1"), ("codex", "renderer=unset flicker=1")] {
        let dir = temp_dir();
        let program = dir.parent().unwrap().join(name);
        std::fs::write(&program, "#!/bin/sh\necho \"renderer=${CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN:-unset} flicker=${CLAUDE_CODE_NO_FLICKER:-unset}\"\nexec cat\n").unwrap();
        std::fs::set_permissions(&program, std::fs::Permissions::from_mode(0o755)).unwrap();
        // The person asked for fullscreen in their own shell: a shared session is classic all the same.
        let env = vec![("PATH".to_string(), "/usr/bin:/bin".to_string()), ("CLAUDE_CODE_NO_FLICKER".into(), "1".into())];
        let argv: Vec<String> = ["/bin/sh", "-c", r#"shift 2; exec "$@""#, "lightbulb-start", "start-1", "/scope", program.to_str().unwrap()].map(String::from).to_vec();
        let info = manager::start_with_env(Path::new(env!("CARGO_BIN_EXE_lightbulb")), &dir, Path::new("/"), &argv, None, Some(env)).unwrap();
        let mut c = UnixStream::connect(&info.socket).unwrap();
        c.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        c.write_all(&encode(HELLO, br#"{"role":"interactive","replayFrom":0}"#)).unwrap();
        read_until(&mut c, renderer.as_bytes());
        manager::stop(info.pid);
    }
}

/// Wait until the holder's screen shows `needle` (drawn before or after any client attached).
fn wait_screen(socket: &Path, needle: &[u8]) {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let mut w = attach(socket, "watch");
        let (kind, snap) = read_frame(&mut w).unwrap();
        if kind == SNAPSHOT && has(&snap, needle) {
            return;
        }
        assert!(Instant::now() < deadline, "the screen never showed {:?}", String::from_utf8_lossy(needle));
        std::thread::sleep(Duration::from_millis(50));
    }
}

/// End session (bug 2026-10-07: End stopped the holder, so "The screen is gone"). The holder ends
/// its provider itself and keeps the last picture, as after `/exit`.
#[test]
fn holder_end_ends_the_provider_and_keeps_its_last_picture() {
    let dir = temp_dir();
    // As Claude Code does on SIGHUP: leave the alternate screen, print the resume lines, exit 129.
    let socket = start(&dir, &["/bin/sh", "-c", concat!(
        r#"trap 'printf "\033[?1000l\033[?1049lResume this session with:\r\n"; exit 129' HUP;"#,
        r" printf '\033[?1049h\033[2J\033[H\033[?1000hlast-answer\r\n> prompt-box'; while :; do sleep 0.1; done")]);
    let mut c = attach(&socket, "interactive");
    wait_screen(&socket, b"prompt-box");
    let info = manager::list_in(&dir).into_iter().next().unwrap();
    assert!(manager::end(&info), "this holder takes END, so it is not stopped");
    let exit = loop {
        let (kind, payload) = read_frame(&mut c).expect("EXIT before timeout");
        if kind == EXIT {
            break serde_json::from_slice::<serde_json::Value>(&payload).unwrap();
        }
    };
    assert_eq!(exit["code"], 129, "the provider was hung up on, as when its terminal closes");
    assert!(manager::is_ended(&manager::list_in(&dir).into_iter().next().expect("the holder stays")));
    let screen = ended_screen(&socket);
    assert!(has(&screen, b"last-answer") && has(&screen, b"> prompt-box"), "{:?}", String::from_utf8_lossy(&screen));
    assert!(!has(&screen, b"Resume this session") && !has(&screen, b"\x1b[?1049h") && !has(&screen, b"\x1b[?1000h"));
    assert!(!manager::end(&info) || manager::is_ended(&info), "a second End changes nothing");
}

/// A provider that ignores the hang-up is killed, and the alternate screen it died on is kept as
/// normal-buffer content.
#[test]
fn holder_end_kills_a_provider_that_ignores_the_hang_up_and_keeps_the_screen_it_died_on() {
    let dir = temp_dir();
    let socket = start(&dir, &["/bin/sh", "-c",
        r"trap '' HUP; printf '\033[?1049h\033[2J\033[H\033[?1000hstuck-screen'; while :; do sleep 0.1; done"]);
    let mut c = attach(&socket, "interactive");
    wait_screen(&socket, b"stuck-screen");
    c.set_read_timeout(Some(run::END_KILL_AFTER + Duration::from_secs(5))).unwrap();
    let asked = Instant::now();
    assert!(manager::end(&manager::list_in(&dir).into_iter().next().unwrap()));
    while read_frame(&mut c).expect("EXIT before timeout").0 != EXIT {}
    assert!(asked.elapsed() >= run::END_KILL_AFTER - Duration::from_millis(200), "it had its time to leave");
    let screen = ended_screen(&socket);
    assert!(has(&screen, b"stuck-screen"), "{:?}", String::from_utf8_lossy(&screen));
    assert!(!has(&screen, b"\x1b[?1049h") && !has(&screen, b"\x1b[?1000h"), "normal-buffer content, mouse off");
}

/// Review of #3818: once End is taken, nothing typed reaches a provider that is slow to leave.
#[test]
fn holder_end_takes_no_more_input_while_the_provider_is_leaving() {
    let dir = temp_dir();
    // Ignores the hang-up and echoes any line it is sent.
    let socket = start(&dir, &["/bin/sh", "-c",
        r#"trap '' HUP; printf '\033[?1049h\033[2J\033[Hwaiting'; while read x; do printf '\r\ngot-%s' "$x"; done"#]);
    let mut c = attach(&socket, "interactive");
    wait_screen(&socket, b"waiting");
    c.set_read_timeout(Some(run::END_KILL_AFTER + Duration::from_secs(5))).unwrap();
    assert!(manager::end(&manager::list_in(&dir).into_iter().next().unwrap()));
    std::thread::sleep(Duration::from_millis(300));
    c.write_all(&encode(INPUT, b"late\r")).unwrap();
    while read_frame(&mut c).expect("EXIT before timeout").0 != EXIT {}
    let screen = ended_screen(&socket);
    assert!(has(&screen, b"waiting") && !has(&screen, b"got-late"), "{:?}", String::from_utf8_lossy(&screen));
}

/// Review of #3818: the provider leaves on the hang-up but a subprocess ignores it. End stops
/// that one too, after the same wait.
#[test]
fn holder_end_also_stops_a_subprocess_that_ignores_the_hang_up() {
    let dir = temp_dir();
    let pidfile = dir.join("child.pid");
    let script = format!(
        r#"(trap '' HUP; while :; do sleep 0.1; done) & echo $! > {}; trap 'exit 129' HUP; printf 'ready'; while :; do sleep 0.1; done"#,
        pidfile.display());
    let socket = start(&dir, &["/bin/sh", "-c", &script]);
    let mut c = attach(&socket, "interactive");
    wait_screen(&socket, b"ready");
    let child: libc::pid_t = std::fs::read_to_string(&pidfile).unwrap().trim().parse().unwrap();
    assert_eq!(unsafe { libc::kill(child, 0) }, 0, "the subprocess runs");
    c.set_read_timeout(Some(run::END_KILL_AFTER + Duration::from_secs(5))).unwrap();
    assert!(manager::end(&manager::list_in(&dir).into_iter().next().unwrap()));
    let until = Instant::now() + run::END_KILL_AFTER + Duration::from_secs(3);
    while unsafe { libc::kill(child, 0) } == 0 && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(100));
    }
    assert_ne!(unsafe { libc::kill(child, 0) }, 0, "End left a subprocess running");
}

/// Bug 2026-10-07: the kept picture after `/exit` was the frame with the slash-command menu.
#[test]
fn holder_keeps_the_picture_from_before_the_command_that_ended_the_session() {
    let dir = temp_dir();
    // Draws a conversation; on the typed line draws a menu over it, then leaves and exits.
    let socket = start(&dir, &["/bin/sh", "-c", concat!(
        r"printf '\033[?1049h\033[2J\033[Hthe-last-answer\r\n> '; read x;",
        r" printf '\033[2;1HExit the CLI menu'; sleep 0.2; printf '\033[?1049lResume this session with:\r\n'; exit 0")]);
    let mut c = attach(&socket, "interactive");
    wait_screen(&socket, b"the-last-answer");
    std::thread::sleep(Duration::from_millis(1700)); // the screen is at rest
    c.write_all(&encode(INPUT, b"/exit")).unwrap();
    std::thread::sleep(Duration::from_millis(300));
    c.write_all(&encode(INPUT, b"\r")).unwrap();
    while read_frame(&mut c).expect("EXIT before timeout").0 != EXIT {}
    let screen = ended_screen(&socket);
    assert!(has(&screen, b"the-last-answer"), "{:?}", String::from_utf8_lossy(&screen));
    assert!(!has(&screen, b"Exit the CLI") && !has(&screen, b"/exit") && !has(&screen, b"Resume this session"), "{:?}", String::from_utf8_lossy(&screen));
}

/// Review of #3818: a prompt whose reply renders quickly, then End within 3 s of its Enter. The
/// End, not the line, ended the session: the kept picture has the reply.
#[test]
fn holder_end_right_after_a_quick_reply_keeps_the_reply() {
    let dir = temp_dir();
    let socket = start(&dir, &["/bin/sh", "-c", concat!(
        r#"trap 'printf "\033[?1049lResume this session with:\r\n"; exit 129' HUP;"#,
        r" printf '\033[?1049h\033[2J\033[Hearlier-answer\r\n> '; read x; printf '\033[3;1Hthe-quick-reply'; while :; do sleep 0.1; done")]);
    let mut c = attach(&socket, "interactive");
    wait_screen(&socket, b"earlier-answer");
    std::thread::sleep(Duration::from_millis(1700)); // the screen is at rest
    c.write_all(&encode(INPUT, b"a question\r")).unwrap();
    wait_screen(&socket, b"the-quick-reply");
    assert!(manager::end(&manager::list_in(&dir).into_iter().next().unwrap()));
    while read_frame(&mut c).expect("EXIT before timeout").0 != EXIT {}
    let screen = ended_screen(&socket);
    assert!(has(&screen, b"the-quick-reply") && has(&screen, b"earlier-answer"), "{:?}", String::from_utf8_lossy(&screen));
    assert!(!has(&screen, b"Resume this session"));
}

/// Review of #3912: the screen relay asks for the lines above the screen on its own connection, and
/// the holder sends the screen right behind them, in one turn of its loop: no output falls between
/// the two, so a viewer drawn from both has every line once.
#[test]
fn holder_answers_history_with_its_lines_and_the_screen_behind_them() {
    let dir = temp_dir();
    let socket = start(&dir, &["/bin/sh", "-c", r#"i=1; while [ $i -le 60 ]; do echo "line $i."; i=$((i+1)); done; exec cat"#]);
    wait_screen(&socket, b"line 60.");
    let mut w = attach(&socket, "watch");
    assert_eq!(read_frame(&mut w).unwrap().0, SNAPSHOT);
    w.write_all(&encode(HISTORY, &with_offset(1 << 16, b""))).unwrap();
    let (kind, lines) = read_frame(&mut w).unwrap();
    assert_eq!(kind, HISTORY);
    let (kind, screen) = read_frame(&mut w).unwrap();
    assert_eq!(kind, SNAPSHOT, "the screen comes behind the lines unasked");
    // 60 lines and the cursor's row on 40 rows: the first 21 are above the screen, the rest on it.
    assert!(has(&lines, b"line 1.") && has(&lines, b"line 21.") && !has(&lines, b"line 22."), "{:?}", String::from_utf8_lossy(&lines));
    assert!(has(&screen, b"line 22.") && has(&screen, b"line 60.") && !has(&screen, b"line 21."));
}
