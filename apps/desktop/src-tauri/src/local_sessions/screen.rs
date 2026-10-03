//! The live terminal relay (spec §5.2, §5.5; rulings 2026-10-03 §4). While the backend's heartbeat
//! ack lists a held session as watched, attach to its holder as a `watch` client and send
//! `local.session.screen` frames on the relay socket: the holder's snapshot first, then output in
//! chunks of at most 11 KiB raw (base64 plus envelope fits the 16 KiB relay frame), each with the
//! terminal's size; a fresh snapshot every 10 s, when the viewer set grows, after a resize and
//! after a lost frame. Nothing is queued for later; nothing is sent while nobody watches.

use super::transport::relay_log;
use super::{process, Mirror, NativeSession, Process};
use crate::holder::manager::{self, HolderInfo};
use crate::holder::protocol::{encode, read_frame, split_offset, EXIT, HELLO, OUTPUT, RESIZE, SNAPSHOT};
use base64::Engine;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::SeqCst};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc;

const CHUNK: usize = 11 * 1024;
const FLUSH: Duration = Duration::from_millis(200);
const SNAPSHOT_EVERY: Duration = Duration::from_secs(10);
/// A relay no heartbeat ack has renewed for this long stops (its relay socket is down).
const LEASE: Duration = Duration::from_secs(30);
const TICK: Duration = Duration::from_millis(50);

#[derive(Clone)]
struct Live {
    generation: String,
    native: NativeSession,
}

static LIVE: Mutex<BTreeMap<String, Live>> = Mutex::new(BTreeMap::new());
static DIR: Mutex<Option<PathBuf>> = Mutex::new(None);
static OUT: Mutex<Option<mpsc::Sender<Value>>> = Mutex::new(None);
static RELAYS: Mutex<BTreeMap<String, Arc<Ctl>>> = Mutex::new(BTreeMap::new());

struct Ctl {
    since: AtomicU64,
    snapshot: AtomicBool,
    stop: AtomicBool,
    until: Mutex<Instant>,
}

impl Ctl {
    fn new(since: u64) -> Self {
        Self { since: AtomicU64::new(since), snapshot: AtomicBool::new(false), stop: AtomicBool::new(false),
            until: Mutex::new(Instant::now() + LEASE) }
    }
    fn renew(&self) {
        if let Ok(mut until) = self.until.lock() {
            *until = Instant::now() + LEASE;
        }
    }
    fn stopped(&self) -> bool {
        self.stop.load(SeqCst) || self.until.lock().map_or(true, |until| Instant::now() >= *until)
    }
}

/// The relay loop's receiving end for this connection; relays send through the newest one.
pub(super) fn channel() -> mpsc::Receiver<Value> {
    let (tx, rx) = mpsc::channel(64);
    if let Ok(mut slot) = OUT.lock() {
        *slot = Some(tx);
    }
    rx
}

/// False when the frame could not be queued: it is gone.
fn send_out(frame: Value) -> bool {
    OUT.lock().ok().and_then(|slot| slot.clone()).is_some_and(|tx| tx.try_send(frame).is_ok())
}

/// The relay loop's live mirrors by session id, refreshed every second.
pub(super) fn index(dir: &Path, mirrors: &BTreeMap<String, Mirror>) {
    let live = mirrors
        .values()
        .filter(|m| m.ended_at.is_none())
        .filter_map(|m| Some((m.session_id.clone()?, Live { generation: m.generation.clone(), native: m.native.clone() })))
        .collect();
    if let Ok(mut slot) = LIVE.lock() {
        *slot = live;
    }
    if let Ok(mut slot) = DIR.lock() {
        *slot = Some(dir.to_path_buf());
    }
}

/// The holder a shared session runs in, and its generation. `HELLO` carries the provider's pid
/// but not its birth, so the birth is read here: the same pid and birth as the mirror's process
/// is that process. Codex's mirror is its rollout writer (a shared daemon on 0.159), never the
/// holder's child, so an app-started session is matched by its start link instead.
pub(crate) fn holder_for_session(session_id: &str) -> Option<(HolderInfo, String)> {
    let live = LIVE.lock().ok()?.get(session_id).cloned()?;
    let dir = DIR.lock().ok()?.clone()?;
    let start = super::start_links::linked(&dir, &live.native, &live.generation);
    let holder = matched(&manager::list(), &live.native.process, start.as_deref(), |pid| process(pid).ok())?;
    Some((holder, live.generation))
}

fn matched(holders: &[HolderInfo], provider: &Process, start: Option<&str>, identity: impl Fn(u32) -> Option<Process>) -> Option<HolderInfo> {
    holders
        .iter()
        .find(|h| h.provider_pid == provider.pid && identity(h.provider_pid).as_ref() == Some(provider))
        .or_else(|| start.and_then(|id| holders.iter().find(|h| h.start_id() == Some(id))))
        .cloned()
}

fn parse(watching: &[Value]) -> BTreeMap<String, u64> {
    watching
        .iter()
        .take(64)
        .filter_map(|e| {
            let sid = e["sessionId"].as_str().filter(|s| uuid::Uuid::parse_str(s).is_ok())?;
            Some((sid.to_owned(), e["since"].as_u64().unwrap_or(0)))
        })
        .collect()
}

/// (start, stop, snapshot) for a new watch list against the running relays' `since`.
fn plan(running: &BTreeMap<String, u64>, wanted: &BTreeMap<String, u64>) -> (Vec<String>, Vec<String>, Vec<String>) {
    let start = wanted.keys().filter(|s| !running.contains_key(*s)).cloned().collect();
    let stop = running.keys().filter(|s| !wanted.contains_key(*s)).cloned().collect();
    let snapshot = wanted
        .iter()
        .filter(|(s, since)| running.get(*s).is_some_and(|was| *since > was))
        .map(|(s, _)| s.clone())
        .collect();
    (start, stop, snapshot)
}

/// A heartbeat ack's `watching`: exactly the sessions of this Mac someone watches now.
pub(super) fn offer(watching: &Value) {
    // Absent: an older backend, or it could not read the list. Running relays keep their lease.
    let Some(list) = watching.as_array() else { return };
    let wanted = parse(list);
    let Ok(mut relays) = RELAYS.lock() else { return };
    relays.retain(|_, ctl| !ctl.stopped());
    let running = relays.iter().map(|(sid, ctl)| (sid.clone(), ctl.since.load(SeqCst))).collect();
    let (start, stop, snapshot) = plan(&running, &wanted);
    for sid in stop {
        if let Some(ctl) = relays.remove(&sid) {
            ctl.stop.store(true, SeqCst);
        }
    }
    for sid in snapshot {
        if let Some(ctl) = relays.get(&sid) {
            ctl.snapshot.store(true, SeqCst);
        }
    }
    for (sid, ctl) in relays.iter() {
        if let Some(since) = wanted.get(sid) {
            ctl.since.store(*since, SeqCst);
            ctl.renew();
        }
    }
    for sid in start {
        let since = wanted[&sid];
        relays.insert(sid.clone(), spawn(sid, since));
    }
}

/// One session's relay on its own thread; the caller keeps the returned control in `RELAYS`.
fn spawn(sid: String, since: u64) -> Arc<Ctl> {
    let ctl = Arc::new(Ctl::new(since));
    let theirs = ctl.clone();
    std::thread::spawn(move || {
        if let Err(e) = run(&sid, &theirs) {
            relay_log(&format!("{} live terminal stopped: {e}", &sid[..8]));
        }
        theirs.stop.store(true, SeqCst);
    });
    ctl
}

fn run(sid: &str, ctl: &Ctl) -> Result<(), String> {
    let (holder, generation) = holder_for_session(sid).ok_or("not a held session")?;
    if !holder.watch {
        return Err("its holder predates live terminals; restart the session to share its screen".into());
    }
    let mut conn = UnixStream::connect(&holder.socket).map_err(|e| e.to_string())?;
    conn.write_all(&encode(HELLO, br#"{"role":"watch"}"#)).map_err(|e| e.to_string())?;
    let mut reader = conn.try_clone().map_err(|e| e.to_string())?;
    let (tx, frames) = std::sync::mpsc::sync_channel(256);
    std::thread::spawn(move || {
        while let Ok(frame) = read_frame(&mut reader) {
            if tx.send(frame).is_err() {
                return;
            }
        }
    });
    let (kind, hello) = frames.recv_timeout(Duration::from_secs(2)).map_err(|_| "the holder did not answer")?;
    if kind != HELLO {
        return Err("the holder answered out of turn".into());
    }
    let facts: Value = serde_json::from_slice(&hello).unwrap_or_default();
    let size = (facts["cols"].as_u64().unwrap_or(120) as u16, facts["rows"].as_u64().unwrap_or(40) as u16);
    let result = pump(&frames, &mut conn, (sid, &generation), size, ctl, &send_out);
    let _ = conn.shutdown(std::net::Shutdown::Both);
    result
}

struct Out<'a> {
    ids: (&'a str, &'a str),
    epoch: u64,
    seq: u64,
    ctl: &'a Ctl,
    send: &'a dyn Fn(Value) -> bool,
}

impl Out<'_> {
    fn emit(&mut self, kind: &str, bytes: &[u8], size: (u16, u16)) {
        for frame in frames_for(self.ids, self.epoch, &mut self.seq, size, kind, bytes) {
            if !(self.send)(frame) {
                // Viewers skip to the next snapshot after a missing frame: make it now.
                self.ctl.snapshot.store(true, SeqCst);
            }
        }
    }
}

/// Holder frames in, relay frames out, until the lease ends, the session exits or the holder goes.
fn pump(frames: &Receiver<(u8, Vec<u8>)>, to_holder: &mut impl Write, ids: (&str, &str), mut size: (u16, u16),
        ctl: &Ctl, send: &dyn Fn(Value) -> bool) -> Result<(), String> {
    let epoch = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64);
    let mut out = Out { ids, epoch, seq: 0, ctl, send };
    let mut pending: Vec<u8> = Vec::new();
    let mut held_since: Option<Instant> = None;
    // The holder sends a watch client its first snapshot unasked.
    let mut last_snapshot = Instant::now();
    loop {
        if ctl.stopped() {
            return Ok(());
        }
        if ctl.snapshot.swap(false, SeqCst) || last_snapshot.elapsed() >= SNAPSHOT_EVERY {
            to_holder.write_all(&encode(SNAPSHOT, b"")).map_err(|e| e.to_string())?;
            last_snapshot = Instant::now();
        }
        match frames.recv_timeout(TICK) {
            Ok((SNAPSHOT, payload)) => {
                let (_, screen) = split_offset(&payload).ok_or("bad snapshot")?;
                // It already shows whatever output was waiting.
                pending.clear();
                held_since = None;
                out.emit("snapshot", screen, size);
            }
            Ok((OUTPUT, payload)) => {
                pending.extend_from_slice(split_offset(&payload).ok_or("bad output")?.1);
                held_since.get_or_insert_with(Instant::now);
            }
            Ok((RESIZE, payload)) if payload.len() == 4 => {
                if !pending.is_empty() {
                    out.emit("output", &std::mem::take(&mut pending), size);
                    held_since = None;
                }
                size = (u16::from_be_bytes([payload[0], payload[1]]), u16::from_be_bytes([payload[2], payload[3]]));
                ctl.snapshot.store(true, SeqCst);
            }
            Ok((EXIT, _)) => {
                if !pending.is_empty() {
                    out.emit("output", &pending, size);
                }
                return Ok(());
            }
            Ok(_) | Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => return Err("the holder closed".into()),
        }
        if pending.len() >= CHUNK || held_since.is_some_and(|at| at.elapsed() >= FLUSH) {
            out.emit("output", &std::mem::take(&mut pending), size);
            held_since = None;
        }
    }
}

/// A snapshot that does not fit one frame continues as "output" frames with the next seqs.
fn frames_for(ids: (&str, &str), epoch: u64, seq: &mut u64, size: (u16, u16), kind: &str, bytes: &[u8]) -> Vec<Value> {
    let chunks: Vec<&[u8]> = if bytes.is_empty() { vec![&[]] } else { bytes.chunks(CHUNK).collect() };
    chunks
        .into_iter()
        .enumerate()
        .map(|(i, chunk)| {
            let frame = json!({"type": "local.session.screen", "sessionId": ids.0, "generation": ids.1,
                "epoch": epoch, "seq": *seq, "cols": size.0, "rows": size.1,
                "kind": if i == 0 { kind } else { "output" },
                "data": base64::engine::general_purpose::STANDARD.encode(chunk)});
            *seq += 1;
            frame
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::holder::protocol::with_offset;
    use std::collections::BTreeSet;

    const GOLDEN: &str =
        include_str!("../../../../../packages/shared/fixtures/local-session-frames/session-screen.json");

    #[test]
    fn screen_frames_have_the_golden_shape_split_at_11_kib_and_fit_a_relay_frame() {
        let generation = "g".repeat(64);
        let mut seq = 7;
        let frames = frames_for(("aaaabbbb-cccc-4ddd-8eee-ffff00001111", &generation), 1_760_000_000_000, &mut seq,
            (120, 40), "snapshot", &vec![0xffu8; 30 * 1024]);
        assert_eq!(frames.iter().map(|f| f["kind"].as_str().unwrap()).collect::<Vec<_>>(), ["snapshot", "output", "output"]);
        assert_eq!(frames.iter().map(|f| f["seq"].as_u64().unwrap()).collect::<Vec<_>>(), [7, 8, 9]);
        assert_eq!(seq, 10);
        let golden: Value = serde_json::from_str(GOLDEN).unwrap();
        let keys = |v: &Value| v.as_object().unwrap().keys().cloned().collect::<BTreeSet<_>>();
        for f in &frames {
            assert_eq!(keys(f), keys(&golden));
            assert!(super::super::ascii_wire_len(f).unwrap() <= 16384);
        }
    }

    #[test]
    fn screen_relay_sends_the_snapshot_then_output_in_order_and_asks_again_after_a_resize() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"\x1b[Hscreen"))).unwrap();
        tx.send((OUTPUT, with_offset(9, &vec![b'a'; 12 * 1024]))).unwrap();
        tx.send((RESIZE, vec![0, 90, 0, 30])).unwrap();
        tx.send((OUTPUT, with_offset(12_297, b"after"))).unwrap();
        tx.send((EXIT, b"{}".to_vec())).unwrap();
        let sent = std::cell::RefCell::new(vec![]);
        let mut to_holder = vec![];
        pump(&rx, &mut to_holder, ("sid", "gen"), (120, 40), &Ctl::new(0), &|f| { sent.borrow_mut().push(f); true }).unwrap();
        let sent = sent.into_inner();
        let shape: Vec<(String, u64)> = sent.iter().map(|f| (f["kind"].as_str().unwrap().into(), f["cols"].as_u64().unwrap())).collect();
        assert_eq!(shape, [("snapshot".into(), 120), ("output".into(), 120), ("output".into(), 120), ("output".into(), 90)]);
        assert_eq!(sent.iter().map(|f| f["seq"].as_u64().unwrap()).collect::<Vec<_>>(), [0, 1, 2, 3]);
        assert!(to_holder.windows(5).any(|w| w == encode(SNAPSHOT, b"").as_slice()), "a resize asks for a fresh screen");
    }

    #[test]
    fn screen_relay_asks_again_after_a_lost_frame_and_stops_when_its_lease_ends() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"screen"))).unwrap();
        let ctl = Ctl::new(0);
        *ctl.until.lock().unwrap() = Instant::now() + Duration::from_millis(300);
        let mut to_holder = vec![];
        let started = Instant::now();
        pump(&rx, &mut to_holder, ("sid", "gen"), (80, 24), &ctl, &|_| false).unwrap();
        assert!(started.elapsed() < Duration::from_secs(2));
        assert!(to_holder.starts_with(&encode(SNAPSHOT, b"")), "the lost snapshot is asked for again");
        drop(tx);
    }

    #[test]
    fn the_watch_list_starts_new_relays_stops_dropped_ones_and_snapshots_when_viewers_grow() {
        let running = BTreeMap::from([("a".to_owned(), 10), ("b".to_owned(), 10)]);
        let wanted = BTreeMap::from([("b".to_owned(), 20), ("c".to_owned(), 5)]);
        assert_eq!(plan(&running, &wanted), (vec!["c".to_owned()], vec!["a".to_owned()], vec!["b".to_owned()]));
        assert_eq!(plan(&running, &running), (vec![], vec![], vec![]));
        let sid = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";
        assert_eq!(parse(&[json!({"sessionId": sid, "since": 3}), json!({"sessionId": "../x", "since": 1})]),
            BTreeMap::from([(sid.to_owned(), 3)]));
    }

    #[test]
    fn a_session_maps_to_the_holder_that_is_its_parent_or_that_was_started_for_it() {
        let holder = |id: &str, provider_pid: u32, argv: &[&str]| HolderInfo {
            id: id.into(), socket: PathBuf::from(format!("/x/{id}.sock")), pid: 1, provider_pid,
            argv: argv.iter().map(|s| s.to_string()).collect(), cwd: "/w".into(), watch: true };
        let holders = [holder("aaaa0001", 500, &["/claude"]),
            holder("aaaa0002", 600, &["/bin/sh", "-c", "x", "lightbulb-start", "start-9", "/scope", "/codex"])];
        let claude = Process { pid: 500, birth_micros: 42, executable: "/claude".into() };
        let alive = |pid: u32| Some(Process { pid, birth_micros: 42, executable: "/claude".into() });
        let reused = |pid: u32| Some(Process { pid, birth_micros: 99, executable: "/claude".into() });
        assert_eq!(matched(&holders, &claude, None, alive).unwrap().id, "aaaa0001");
        assert!(matched(&holders, &claude, None, reused).is_none(), "a reused pid is not the session");
        let daemon = Process { pid: 777, birth_micros: 1, executable: "/daemon".into() };
        assert_eq!(matched(&holders, &daemon, Some("start-9"), alive).unwrap().id, "aaaa0002");
        assert!(matched(&holders, &daemon, None, alive).is_none(), "a plain terminal session has no holder");
    }
}
