//! Remote typing (owner ruling 2026-10-03, Task 9). The backend forwards an admitted teammate's
//! keystrokes as `local.session.input`; this writes them to the session's holder as its one
//! `type` client. Never answered, whatever arrives. Dropped unless the session is in the live
//! relay set at that generation (someone watches: nobody types blind), the epoch is the relay's
//! current one, and the holder takes `type` clients. Terminal probe replies are stripped first.
//! The connection task only queues: the holder lookup and the writes run on the relay's own
//! writer thread, which closes its `type` client when the relay stops.

use super::screen::{self, Ctl};
use crate::holder::protocol::{encode, read_frame, HELLO, INPUT};
use base64::Engine;
use serde_json::Value;
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::sync::atomic::Ordering::SeqCst;
use std::sync::mpsc::{sync_channel, Receiver, RecvTimeoutError, TrySendError};
use std::sync::Arc;
use std::time::{Duration, Instant};

/// The backend's per-call bound on raw bytes.
const MAX_INPUT: usize = 4096;
/// Calls queued for one holder; more are dropped (the backend already rate-bounds them).
const QUEUE: usize = 64;
const TICK: Duration = Duration::from_millis(50);
/// After a failed attach, inputs are dropped this long before trying again.
const RETRY: Duration = Duration::from_secs(1);

/// A `local.session.input` frame from the relay. Never blocks, never replies.
pub(super) fn offer(body: &Value) {
    let Some((sid, generation, ctl, mut keys)) = admit(body, screen::live_relay) else { return };
    let Ok(mut slot) = ctl.typist.lock() else { return };
    if let Some(tx) = slot.as_ref() {
        match tx.try_send(keys) {
            Ok(()) | Err(TrySendError::Full(_)) => return,
            // Its writer is gone: start another.
            Err(TrySendError::Disconnected(back)) => keys = back,
        }
    }
    let (tx, rx) = sync_channel(QUEUE);
    let _ = tx.try_send(keys);
    *slot = Some(tx);
    let theirs = ctl.clone();
    std::thread::spawn(move || write(&sid, &generation, &theirs, &rx));
}

/// (session id, generation, its relay, the keys to write) when the frame may reach the holder.
fn admit(body: &Value, relay_of: impl Fn(&Value) -> Option<(String, Arc<Ctl>)>) -> Option<(String, String, Arc<Ctl>, Vec<u8>)> {
    let data = body["data"].as_str().filter(|d| d.len() <= MAX_INPUT.div_ceil(3) * 4)?;
    let epoch = body["epoch"].as_u64().filter(|e| *e != 0)?;
    let generation = body["generation"].as_str()?.to_owned();
    let (sid, ctl) = relay_of(body)?;
    // A join request is waiting for the owner: their band has Allow on it, and a teammate's
    // key on this pty could press it. Dropped, not queued.
    if super::presence::frozen(&sid, &generation) {
        return None;
    }
    if ctl.epoch.load(SeqCst) != epoch {
        return None;
    }
    let raw = base64::engine::general_purpose::STANDARD.decode(data).ok().filter(|r| r.len() <= MAX_INPUT)?;
    let keys = strip_probe_replies(&raw);
    (!keys.is_empty()).then_some((sid, generation, ctl, keys))
}

/// The relay's writer: one `type` client, reattached after an error, closed when the relay stops.
fn write(sid: &str, generation: &str, ctl: &Ctl, inputs: &Receiver<Vec<u8>>) {
    let mut conn: Option<UnixStream> = None;
    let mut tried: Option<Instant> = None;
    while !ctl.stopped() {
        let keys = match inputs.recv_timeout(TICK) {
            Ok(keys) => keys,
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => break,
        };
        for _ in 0..2 {
            if conn.is_none() && tried.is_none_or(|at| at.elapsed() >= RETRY) {
                tried = Some(Instant::now());
                conn = attach(sid, generation);
            }
            let Some(c) = conn.as_mut() else { break };
            if c.write_all(&encode(INPUT, &keys)).is_ok() {
                break;
            }
            // The holder went away or restarted: reattach once, now.
            conn = None;
            tried = None;
        }
    }
    if let Some(c) = conn {
        let _ = c.shutdown(std::net::Shutdown::Both);
    }
}

/// A `type` client on the session's holder, only when its HELLO says it takes one.
fn attach(sid: &str, generation: &str) -> Option<UnixStream> {
    let (holder, current) = screen::holder_for_session(sid, false)?;
    if current != generation || !holder.type_input {
        return None;
    }
    let mut conn = UnixStream::connect(&holder.socket).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.set_write_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.write_all(&encode(HELLO, br#"{"role":"type"}"#)).ok()?;
    let (kind, facts) = read_frame(&mut conn).ok()?;
    let facts: Value = serde_json::from_slice(&facts).ok()?;
    (kind == HELLO && facts["type"] == true).then_some(conn)
}

/// The bytes minus the replies a terminal (xterm.js) sends to queries: DA, CPR, DSR, DECRPM,
/// kitty keyboard flags, focus, OSC 4/10/11/12 colours, XTVERSION and DECRQSS. The holder
/// answers queries itself, so a remote viewer's answers would reach the provider as typing.
/// Known cost: Shift/Ctrl+F3 (`ESC [ 1 ; n R`) reads as a CPR and is dropped.
pub(super) fn strip_probe_replies(input: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(input.len());
    let mut i = 0;
    while i < input.len() {
        let reply = match (input[i], input.get(i + 1)) {
            (0x1b, Some(b'[')) => csi_reply(&input[i + 2..]),
            (0x1b, Some(&kind @ (b']' | b'P'))) => string_reply(kind, &input[i + 2..]),
            _ => None,
        };
        match reply {
            Some(len) => i += 2 + len,
            None => {
                out.push(input[i]);
                i += 1;
            }
        }
    }
    out
}

/// The length of a CSI after `ESC [` when it is a query reply; None for a key (or incomplete).
fn csi_reply(rest: &[u8]) -> Option<usize> {
    let params = rest.iter().take_while(|b| (0x30..=0x3f).contains(*b)).count();
    let inter = rest[params..].iter().take_while(|b| (0x20..=0x2f).contains(*b)).count();
    let last = *rest.get(params + inter).filter(|b| (0x40..=0x7e).contains(*b))?;
    let (p, i) = (&rest[..params], &rest[params..params + inter]);
    let numeric = |s: &[u8]| !s.is_empty() && s.iter().all(|b| b.is_ascii_digit() || *b == b';');
    let reply = match (last, i) {
        (b'c', []) => matches!(p.first(), Some(b'?' | b'>')),
        (b'R', []) => p.contains(&b';') && numeric(p.strip_prefix(b"?".as_slice()).unwrap_or(p)),
        (b'n', []) => p == b"0",
        (b'y', [b'$']) => true,
        (b'u', []) => p.first() == Some(&b'?'),
        (b'I' | b'O', []) => p.is_empty(),
        _ => false,
    };
    reply.then_some(params + inter + 1)
}

/// The length of an OSC (`]`) or DCS (`P`) after its introducer, through BEL or `ESC \`, when it
/// is a query reply; None otherwise or when it is not terminated here.
fn string_reply(kind: u8, rest: &[u8]) -> Option<usize> {
    let end = rest.iter().position(|b| *b == 0x07 || *b == 0x1b)?;
    let term = match rest[end] {
        0x07 => 1,
        _ if rest.get(end + 1) == Some(&b'\\') => 2,
        _ => return None,
    };
    let replies: &[&[u8]] = if kind == b']' { &[b"4;", b"10;", b"11;", b"12;"] } else { &[b">|", b"1$r", b"0$r"] };
    replies.iter().any(|r| rest[..end].starts_with(r)).then_some(end + term)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::collections::BTreeMap;

    #[test]
    fn remote_input_strips_terminal_probe_replies_and_keeps_typing() {
        let table: &[(&[u8], &[u8])] = &[
            (b"\x1b[?62;22c", b""),
            (b"a\x1b[>0;276;0cb", b"ab"),
            (b"\x1b[12;40R", b""),
            (b"\x1b[?12;40R", b""),
            (b"\x1b[0n", b""),
            (b"\x1b[?2004;1$y\x1b[4;2$y", b""),
            (b"\x1b[?1u", b""),
            (b"\x1b[I\x1b[O", b""),
            (b"\x1b]11;rgb:1616/1616/1a1a\x1b\\", b""),
            (b"\x1b]10;rgb:0/0/0\x07x", b"x"),
            (b"\x1b]4;1;rgb:cdcd/0000/0000\x07", b""),
            (b"\x1bP>|xterm.js(5.5.0)\x1b\\", b""),
            (b"h\x1b[?1;2ci", b"hi"),
            // Typing, kept as is.
            (b"ls -la\r", b"ls -la\r"),
            (b"\x1b[A\x1b[1;5D\x1bOP", b"\x1b[A\x1b[1;5D\x1bOP"),
            (b"\x1b[200~paste\x1b[201~", b"\x1b[200~paste\x1b[201~"),
            (b"\x1b\r", b"\x1b\r"),
            (b"\x1b[", b"\x1b["),
            (b"\x1b]", b"\x1b]"),
            (b"\x1b]11;rgb:0/0/0", b"\x1b]11;rgb:0/0/0"),
            (b"\x1b[5n", b"\x1b[5n"),
            ("caf\u{e9} \u{1f600}".as_bytes(), "caf\u{e9} \u{1f600}".as_bytes()),
        ];
        for (input, kept) in table {
            assert_eq!(strip_probe_replies(input), *kept, "{input:?}");
        }
    }

    const SID: &str = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";

    fn frame(sid: &str, generation: &str, epoch: u64, data: &[u8]) -> Value {
        json!({"type": "local.session.input", "sessionId": sid, "generation": generation, "epoch": epoch,
            "data": base64::engine::general_purpose::STANDARD.encode(data)})
    }

    #[test]
    fn remote_input_is_dropped_unless_watched_at_this_generation_and_the_current_epoch() {
        let _g = super::super::presence::TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let ctl = Arc::new(Ctl::new(0));
        ctl.epoch.store(7, SeqCst);
        let live = BTreeMap::from([(SID.to_owned(), "gen-1".to_owned())]);
        // `screen::live_relay` with the live mirrors and running relays given.
        let relay = |relays: BTreeMap<String, Arc<Ctl>>| {
            let live = live.clone();
            move |body: &Value| {
                let sid = screen::nudged(body, |s| live.get(s).cloned())?;
                Some((sid.clone(), relays.get(&sid)?.clone()))
            }
        };
        let watched = || relay(BTreeMap::from([(SID.to_owned(), ctl.clone())]));
        let (sid, generation, _, keys) = admit(&frame(SID, "gen-1", 7, b"ls\r\x1b[?62;22c"), watched()).unwrap();
        assert_eq!((sid.as_str(), generation.as_str(), keys.as_slice()), (SID, "gen-1", b"ls\r".as_slice()));
        assert!(admit(&frame(SID, "gen-1", 7, b"ls\r"), relay(BTreeMap::new())).is_none(), "nobody watches it");
        assert!(admit(&frame(SID, "gen-0", 7, b"ls\r"), watched()).is_none(), "another generation");
        assert!(admit(&frame(SID, "gen-1", 6, b"ls\r"), watched()).is_none(), "a stale epoch");
        assert!(admit(&frame("bbbbcccc-dddd-4eee-8fff-000011112222", "gen-1", 7, b"ls\r"), watched()).is_none(), "not this Mac's");
        assert!(admit(&frame(SID, "gen-1", 7, &[b'x'; MAX_INPUT + 1]), watched()).is_none(), "over the bound");
        assert!(admit(&frame(SID, "gen-1", 7, b"\x1b[12;40R"), watched()).is_none(), "nothing left to type");
        assert!(admit(&json!({"type": "local.session.input", "sessionId": SID, "generation": "gen-1", "epoch": 7, "data": "%%"}), watched()).is_none());
        ctl.epoch.store(0, SeqCst);
        assert!(admit(&frame(SID, "gen-1", 0, b"ls\r"), watched()).is_none(), "a relay that has not started streaming");
    }

    #[test]
    fn remote_input_is_dropped_while_a_join_request_waits() {
        use super::super::presence::{set, Entry, JoinRequest, TEST_LOCK};
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sid = "cccc1111-2222-4333-8444-555566667777";
        let ctl = Arc::new(Ctl::new(0));
        ctl.epoch.store(7, SeqCst);
        let relay = |body: &Value| {
            let _ = body;
            Some((sid.to_owned(), ctl.clone()))
        };
        let mut e = Entry { generation: "gen-1".into(), paused: false, viewers: vec![], join_requests: vec![], at: 0 };
        set(sid, e.clone());
        assert!(admit(&frame(sid, "gen-1", 7, b"ls\r"), relay).is_some());
        e.join_requests = vec![JoinRequest { user_id: "u2".into(), name: "Mira".into(), request_id: "r1".into(), kind: "join".into() }];
        set(sid, e);
        assert!(admit(&frame(sid, "gen-1", 7, b"ls\r"), relay).is_none());
        assert!(admit(&frame(sid, "gen-2", 7, b"ls\r"), relay).is_some(), "the old generation's request does not freeze a restart");
    }

    #[test]
    fn remote_input_writer_closes_its_type_client_when_the_relay_stops() {
        let ctl = Ctl::new(0);
        ctl.stop.store(true, SeqCst);
        let (tx, rx) = sync_channel(1);
        tx.send(b"never".to_vec()).unwrap();
        let started = Instant::now();
        write(SID, "gen-1", &ctl, &rx);
        assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(rx.try_recv().unwrap(), b"never", "nothing is written once the relay stopped");
    }
}
