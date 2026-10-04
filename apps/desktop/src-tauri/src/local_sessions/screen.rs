//! The live terminal relay (spec §5.2, §5.5; rulings 2026-10-03 §4). While the backend's heartbeat
//! ack lists a held session as watched, attach to its holder as a `watch` client and send
//! `local.session.screen` frames on the relay socket: the holder's snapshot first, then output in
//! chunks of at most 11 KiB raw (base64 plus envelope fits the 16 KiB relay frame), each with the
//! terminal's size; a fresh snapshot every 10 s, when the viewer set grows, after a resize and
//! after a lost frame (at most one a second). Nothing is queued for later; nothing is sent while
//! nobody watches. Output past the backend's budget (2 MiB raw a minute) is dropped here, not
//! sent to be dropped there, and a snapshot follows once the budget is back.
//! A `local.session.watch` nudge from the backend starts a relay at once (a new viewer sees the
//! screen in about a second, not at the next ack); the acks still keep it going and stop it.
//! A Claude background session's terminal comes from an attach holder this module starts on demand
//! and stops when nobody uses it, or when discovery no longer lists its job as running.

use super::transport::relay_log;
use super::{Mirror, NativeSession};
use crate::holder::manager::{self, HolderInfo};
use crate::holder::protocol::{encode, read_frame, split_offset, EXIT, HELLO, OUTPUT, RESIZE, SNAPSHOT};
use base64::Engine;
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::os::unix::net::UnixStream;
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
/// A nudged relay outlives acks that miss it for this long: an ack the backend built just before
/// the viewer arrived can land after the nudge. Longer than one 10 s heartbeat.
const GRACE: Duration = Duration::from_secs(15);
/// Snapshot requests are at most this often: a lost frame on a slow uplink must not become a
/// full screen every tick.
const SNAPSHOT_GAP: Duration = Duration::from_secs(1);
/// The backend's per-session budget (`session_screen.BUDGET_BYTES_PER_MINUTE`).
const BUDGET_PER_MINUTE: usize = 2 * 1024 * 1024;
const BURST: usize = 256 * 1024;

#[derive(Clone)]
struct Live {
    generation: String,
    native: NativeSession,
}

static LIVE: Mutex<BTreeMap<String, Live>> = Mutex::new(BTreeMap::new());
static OUT: Mutex<Option<mpsc::Sender<Value>>> = Mutex::new(None);
static RELAYS: Mutex<BTreeMap<String, Arc<Ctl>>> = Mutex::new(BTreeMap::new());

pub(super) struct Ctl {
    since: AtomicU64,
    /// The epoch `pump` stamps on this relay's frames; 0 until it starts. Remote input must
    /// carry it (`remote_input`): typing aimed at an older screen is dropped.
    pub(super) epoch: AtomicU64,
    /// This relay's one `type` client to the holder (`remote_input`); it closes when we stop.
    pub(super) typist: Mutex<Option<std::sync::mpsc::SyncSender<Vec<u8>>>>,
    snapshot: AtomicBool,
    pub(super) stop: AtomicBool,
    until: Mutex<Instant>,
    /// Absence from `watching` does not stop the relay before this (a nudge sets it).
    keep: Mutex<Instant>,
}

impl Ctl {
    pub(super) fn new(since: u64) -> Self {
        Self { since: AtomicU64::new(since), epoch: AtomicU64::new(0), typist: Mutex::new(None), snapshot: AtomicBool::new(false), stop: AtomicBool::new(false),
            until: Mutex::new(Instant::now() + LEASE), keep: Mutex::new(Instant::now()) }
    }
    fn renew(&self) {
        if let Ok(mut until) = self.until.lock() {
            *until = Instant::now() + LEASE;
        }
    }
    fn nudged(&self) {
        self.renew();
        if let Ok(mut keep) = self.keep.lock() {
            *keep = Instant::now() + GRACE;
        }
    }
    fn kept(&self) -> bool {
        self.keep.lock().is_ok_and(|keep| Instant::now() < *keep)
    }
    pub(super) fn stopped(&self) -> bool {
        self.stop.load(SeqCst) || self.until.lock().map_or(true, |until| Instant::now() >= *until)
    }
}

/// The relay loop's receiving end for this connection; relays send through the newest one.
pub(super) fn channel() -> mpsc::Receiver<Value> {
    // Shallow: these frames go out ahead of nothing, and a deep queue on a slow uplink sits in
    // front of every heartbeat and command answer. A full queue is a lost frame (a snapshot later).
    let (tx, rx) = mpsc::channel(8);
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
pub(super) fn index(mirrors: &BTreeMap<String, Mirror>) {
    let live = mirrors
        .values()
        .filter(|m| m.ended_at.is_none())
        .filter_map(|m| Some((m.session_id.clone()?, Live { generation: m.generation.clone(), native: m.native.clone() })))
        .collect();
    if let Ok(mut slot) = LIVE.lock() {
        *slot = live;
    }
}

/// An attach holder unused this long (not in the panel, no relay) is closed.
const IDLE: Duration = Duration::from_secs(60);
/// At most one attach start per job this often: a client that exits at once is not restarted in a loop.
const RESTART_GAP: Duration = Duration::from_secs(30);
/// Attach holder id -> when it was last asked for.
static WANTED: Mutex<BTreeMap<String, Instant>> = Mutex::new(BTreeMap::new());
/// Job -> when an attach holder was last started for it. Held across list+start, so the panel and a
/// relay asking together share one holder.
static STARTED: Mutex<BTreeMap<String, Instant>> = Mutex::new(BTreeMap::new());

/// The holder that shows a session's live terminal, and its generation. A held session's own
/// holder (`track`: Claude as the holder's own child, Codex by the thread its holder bound); for a
/// Claude background session, Lightbulb's attach holder, started on demand (rulings 2026-10-04).
/// `panel`: the owner's own panel asks, the one caller that may size the job's terminal. None: no
/// live terminal (the transcript).
pub(crate) fn holder_for_session(session_id: &str, panel: bool) -> Option<(HolderInfo, String)> {
    let live = LIVE.lock().ok()?.get(session_id).cloned()?;
    let holder = match (live.native.holder.as_deref(), live.native.job.as_deref()) {
        (Some(id), _) => matched(manager::list(), id)?,
        (None, Some(job)) => {
            let pid = live.native.process.pid;
            let holder = attach_holder(job, manager::list, |job| start_size(tty_size(pid), panel).map(|size| attach_start(job, size)))?;
            if let Ok(mut wanted) = WANTED.lock() {
                wanted.insert(holder.id.clone(), Instant::now());
            }
            holder
        }
        _ => return None,
    };
    Some((holder, live.generation))
}

/// The running attach holder for `job`, or a new one. `start` answers None when this caller may
/// not start one now (that spends no restart gap).
fn attach_holder(job: &str, list: impl Fn() -> Vec<HolderInfo>, start: impl Fn(&str) -> Option<Result<HolderInfo, String>>) -> Option<HolderInfo> {
    let mut started = STARTED.lock().ok()?;
    if let Some(holder) = list().into_iter().find(|h| manager::attach_job(h) == Some(job)) {
        return Some(holder);
    }
    if started.get(job).is_some_and(|at| at.elapsed() < RESTART_GAP) {
        return None;
    }
    let attempt = start(job)?;
    started.insert(job.to_owned(), Instant::now());
    attempt.map_err(|e| relay_log(&format!("attach {job}: {e}"))).ok()
}

/// The size an attach holder starts at: the job's current size, so watching never resizes the
/// owner's terminal (Task 0: the job draws at the latest attach size). Unknown: only the owner's
/// panel may start one (it sizes it itself); a viewer or a typist gets None.
fn start_size(job_size: Option<(u16, u16)>, panel: bool) -> Option<Option<(u16, u16)>> {
    (job_size.is_some() || panel).then_some(job_size)
}

/// The size of the terminal process `pid` runs in: its controlling tty's window size.
fn tty_size(pid: u32) -> Option<(u16, u16)> {
    use std::os::unix::fs::OpenOptionsExt;
    use std::os::unix::io::AsRawFd;
    let out = std::process::Command::new("/bin/ps").args(["-o", "tty=", "-p", &pid.to_string()]).output().ok()?;
    let name = String::from_utf8(out.stdout).ok()?.trim().to_owned();
    // "??" is no terminal; anything but a plain device name is not read.
    if !name.starts_with("tty") || !name.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return None;
    }
    let tty = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOCTTY | libc::O_NONBLOCK)
        .open(format!("/dev/{name}"))
        .ok()?;
    let mut size: libc::winsize = unsafe { std::mem::zeroed() };
    if unsafe { libc::ioctl(tty.as_raw_fd(), libc::TIOCGWINSZ, &mut size) } != 0 || size.ws_col == 0 || size.ws_row == 0 {
        return None;
    }
    Some((size.ws_col, size.ws_row))
}

fn attach_start(job: &str, size: Option<(u16, u16)>) -> Result<HolderInfo, String> {
    let home = std::env::var_os("HOME").map(std::path::PathBuf::from).ok_or("no home folder")?;
    let claude = super::provider_program(&home, "claude").ok_or("Claude Code is not installed")?;
    manager::start_sized(&home, &manager::attach_argv(&claude, job), size)
}

/// The attach holders to stop: their job is no longer listed as running (`running`, None when the
/// listing failed: no evidence), or nobody shows, watches or asked for them in `IDLE`. First sight
/// counts as asked, so holders found after an app restart get a grace period.
fn idle_attach(holders: &[HolderInfo], shown: Option<&str>, watched_jobs: &BTreeSet<String>, running: Option<&BTreeSet<String>>,
               wanted: &mut BTreeMap<String, Instant>, now: Instant) -> Vec<HolderInfo> {
    wanted.retain(|id, _| holders.iter().any(|h| &h.id == id));
    holders
        .iter()
        .filter(|h| {
            let Some(job) = manager::attach_job(h) else { return false };
            // Shown or watched now counts as asked now: IDLE runs from the last use, not the last lookup.
            let used = shown == Some(h.id.as_str()) || watched_jobs.contains(job);
            let at = wanted.entry(h.id.clone()).or_insert(now);
            if used {
                *at = now;
            }
            let ended = running.is_some_and(|r| !r.contains(job));
            ended || (!used && now.duration_since(*at) >= IDLE)
        })
        .cloned()
        .collect()
}

/// Close attach holders nobody uses or whose job is no longer running. Ends only the `claude
/// attach` client; the background session runs on under Claude's own daemon. Never `claude stop`.
pub(super) fn reap_attach(holders: &[HolderInfo], running: Option<&BTreeSet<String>>) {
    let shown = crate::holder::panel::shown_holder();
    let live = LIVE.lock().map(|l| l.clone()).unwrap_or_default();
    let watched: BTreeSet<String> = RELAYS
        .lock()
        .map(|r| r.iter().filter(|(_, c)| !c.stopped()).filter_map(|(sid, _)| live.get(sid)?.native.job.clone()).collect())
        .unwrap_or_default();
    let Ok(mut wanted) = WANTED.lock() else { return };
    for holder in idle_attach(holders, shown.as_deref(), &watched, running, &mut wanted, Instant::now()) {
        manager::stop_attach(&holder);
    }
}

fn matched(holders: Vec<HolderInfo>, id: &str) -> Option<HolderInfo> {
    holders.into_iter().find(|h| h.id == id)
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
    settle(&mut relays, &wanted, spawn);
}

fn settle(relays: &mut BTreeMap<String, Arc<Ctl>>, wanted: &BTreeMap<String, u64>, spawn: impl Fn(String, u64) -> Arc<Ctl>) {
    relays.retain(|_, ctl| !ctl.stopped());
    let running = relays.iter().map(|(sid, ctl)| (sid.clone(), ctl.since.load(SeqCst))).collect();
    let (start, stop, snapshot) = plan(&running, wanted);
    for sid in stop {
        if relays.get(&sid).is_some_and(|ctl| ctl.kept()) {
            continue;
        }
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

/// A backend `local.session.watch`: someone just started watching. Never answered; anything this
/// Mac cannot stream (not its session, another generation, ended) is ignored.
pub(super) fn nudge(body: &Value) {
    let Some(sid) = LIVE.lock().ok().and_then(|live| nudged(body, |sid| live.get(sid).map(|l| l.generation.clone())))
    else { return };
    let Ok(mut relays) = RELAYS.lock() else { return };
    relays.retain(|_, ctl| !ctl.stopped());
    nudge_relay(&mut relays, sid, spawn);
}

/// The running relay of a session this Mac holds at `body`'s generation: what remote input
/// may reach. None when nobody watches it (nobody types blind), or it is another generation.
pub(super) fn live_relay(body: &Value) -> Option<(String, Arc<Ctl>)> {
    let sid = LIVE.lock().ok().and_then(|live| nudged(body, |sid| live.get(sid).map(|l| l.generation.clone())))?;
    let ctl = RELAYS.lock().ok()?.get(&sid).filter(|ctl| !ctl.stopped())?.clone();
    Some((sid, ctl))
}

/// The nudged session id, when `generation_of` (the live mirrors) knows it at that generation.
pub(super) fn nudged(body: &Value, generation_of: impl Fn(&str) -> Option<String>) -> Option<String> {
    let sid = body["sessionId"].as_str().filter(|s| uuid::Uuid::parse_str(s).is_ok())?;
    (generation_of(sid)? == body["generation"].as_str()?).then(|| sid.to_owned())
}

fn nudge_relay(relays: &mut BTreeMap<String, Arc<Ctl>>, sid: String, spawn: impl Fn(String, u64) -> Arc<Ctl>) {
    if let Some(ctl) = relays.get(&sid) {
        // Running already: the new viewer needs a screen now, not in up to 10 s.
        ctl.snapshot.store(true, SeqCst);
        ctl.nudged();
        return;
    }
    // The holder snapshots a new watch client unasked. `since` is unknown until an ack lists it;
    // MAX keeps that first ack from asking for a second snapshot.
    let ctl = spawn(sid.clone(), u64::MAX);
    ctl.nudged();
    relays.insert(sid, ctl);
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
    let (holder, generation) = holder_for_session(sid, false).ok_or("not a held session")?;
    if !holder.watch {
        return Err("its holder predates live terminals; restart the session to share its screen".into());
    }
    let mut conn = UnixStream::connect(&holder.socket).map_err(|e| e.to_string())?;
    let result = watch(&mut conn, sid, &generation, ctl, &send_out);
    // Every exit, early ones included: the reader thread's clone is unblocked only by this.
    let _ = conn.shutdown(std::net::Shutdown::Both);
    result
}

fn watch(conn: &mut UnixStream, sid: &str, generation: &str, ctl: &Ctl, send: &dyn Fn(Value) -> bool) -> Result<(), String> {
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
    pump(&frames, conn, (sid, generation), size, ctl, send, Budget::new())
}

/// Raw screen bytes this relay may still send: refills at the backend's rate, holds a burst.
struct Budget {
    tokens: f64,
    cap: f64,
    per_s: f64,
    at: Instant,
}

impl Budget {
    fn new() -> Self {
        Self { tokens: BURST as f64, cap: BURST as f64, per_s: BUDGET_PER_MINUTE as f64 / 60.0, at: Instant::now() }
    }
    fn left(&mut self) -> f64 {
        let now = Instant::now();
        self.tokens = (self.tokens + now.duration_since(self.at).as_secs_f64() * self.per_s).min(self.cap);
        self.at = now;
        self.tokens
    }
    /// Output only when it fits. A snapshot always goes (it may overdraw): it ends a gap, and
    /// the next request waits for the budget to be back above zero.
    fn take(&mut self, bytes: usize, overdraw: bool) -> bool {
        if !overdraw && self.left() < bytes as f64 {
            return false;
        }
        self.left();
        self.tokens -= bytes as f64;
        true
    }
}

struct Out<'a> {
    ids: (&'a str, &'a str),
    epoch: u64,
    seq: u64,
    ctl: &'a Ctl,
    send: &'a dyn Fn(Value) -> bool,
    budget: Budget,
    /// A frame was lost since the last snapshot: viewers wait for the next one, so output is waste.
    stale: bool,
}

impl Out<'_> {
    fn emit(&mut self, kind: &str, bytes: &[u8], size: (u16, u16)) {
        let snapshot = kind == "snapshot";
        if self.stale && !snapshot {
            return;
        }
        if !self.budget.take(bytes.len(), snapshot) {
            // Skip the seqs it would have used: the gap tells viewers to wait for a snapshot.
            self.seq += bytes.len().div_ceil(CHUNK).max(1) as u64;
            self.lost();
            return;
        }
        self.stale = false;
        for frame in frames_for(self.ids, self.epoch, &mut self.seq, size, kind, bytes) {
            if !(self.send)(frame) {
                self.lost();
            }
        }
    }
    fn lost(&mut self) {
        // Viewers skip to the next snapshot after a missing frame: ask for one.
        self.stale = true;
        self.ctl.snapshot.store(true, SeqCst);
    }
}

static LAST_EPOCH: AtomicU64 = AtomicU64::new(0);

/// Wall-clock milliseconds, but never equal to or below an earlier epoch: a viewer drops a lower one as stale,
/// and a clock step back or two relays in one millisecond would otherwise freeze it.
fn next_epoch(last: &AtomicU64, now_ms: u64) -> u64 {
    let prev = last.fetch_update(SeqCst, SeqCst, |l| Some(now_ms.max(l + 1))).unwrap_or(0);
    now_ms.max(prev + 1)
}

/// Holder frames in, relay frames out, until the lease ends, the session exits or the holder goes.
fn pump(frames: &Receiver<(u8, Vec<u8>)>, to_holder: &mut impl Write, ids: (&str, &str), mut size: (u16, u16),
        ctl: &Ctl, send: &dyn Fn(Value) -> bool, budget: Budget) -> Result<(), String> {
    let epoch = next_epoch(&LAST_EPOCH, SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64));
    ctl.epoch.store(epoch, SeqCst);
    let mut out = Out { ids, epoch, seq: 0, ctl, send, budget, stale: false };
    let mut pending: Vec<u8> = Vec::new();
    let mut held_since: Option<Instant> = None;
    // The holder sends a watch client its first snapshot unasked; a request may follow at once
    // (a resize, a lost frame), and the periodic one comes a gap early.
    let mut last_snapshot = Instant::now().checked_sub(SNAPSHOT_GAP).unwrap_or_else(Instant::now);
    loop {
        if ctl.stopped() {
            return Ok(());
        }
        let due = ctl.snapshot.load(SeqCst) || last_snapshot.elapsed() >= SNAPSHOT_EVERY;
        if due && last_snapshot.elapsed() >= SNAPSHOT_GAP && out.budget.left() > 0.0 {
            ctl.snapshot.store(false, SeqCst);
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

    #[test]
    fn epochs_increase_in_one_millisecond_and_when_the_clock_steps_back() {
        let last = AtomicU64::new(0);
        let a = next_epoch(&last, 1000);
        let b = next_epoch(&last, 1000);
        let c = next_epoch(&last, 900);
        assert!(a < b && b < c);
    }

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
        pump(&rx, &mut to_holder, ("sid", "gen"), (120, 40), &Ctl::new(0), &|f| { sent.borrow_mut().push(f); true }, Budget::new()).unwrap();
        let sent = sent.into_inner();
        let shape: Vec<(String, u64)> = sent.iter().map(|f| (f["kind"].as_str().unwrap().into(), f["cols"].as_u64().unwrap())).collect();
        assert_eq!(shape, [("snapshot".into(), 120), ("output".into(), 120), ("output".into(), 120), ("output".into(), 90)]);
        assert_eq!(sent.iter().map(|f| f["seq"].as_u64().unwrap()).collect::<Vec<_>>(), [0, 1, 2, 3]);
        assert!(to_holder.windows(5).any(|w| w == encode(SNAPSHOT, b"").as_slice()), "a resize asks for a fresh screen");
    }

    /// A holder that answers every snapshot request with `answer` and notes when it was asked.
    struct Answering {
        tx: std::sync::mpsc::Sender<(u8, Vec<u8>)>,
        answer: Vec<(u8, Vec<u8>)>,
        asked: Vec<Instant>,
    }

    impl Write for Answering {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            assert_eq!(buf, encode(SNAPSHOT, b"").as_slice());
            self.asked.push(Instant::now());
            for frame in &self.answer {
                let _ = self.tx.send(frame.clone());
            }
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    fn leased(for_: Duration) -> Ctl {
        let ctl = Ctl::new(0);
        *ctl.until.lock().unwrap() = Instant::now() + for_;
        ctl
    }

    #[test]
    fn screen_relay_asks_again_after_a_lost_frame_at_most_once_a_second_and_stops_when_its_lease_ends() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"screen"))).unwrap();
        let ctl = leased(Duration::from_millis(2500));
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(0, b"screen"))], asked: vec![] };
        let started = Instant::now();
        // Every frame is lost, so every snapshot asks for the next one.
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|_| false, Budget::new()).unwrap();
        assert!(started.elapsed() < Duration::from_secs(4));
        assert!((2..=3).contains(&holder.asked.len()), "asked {} times, not every tick", holder.asked.len());
        for pair in holder.asked.windows(2) {
            assert!(pair[1] - pair[0] >= SNAPSHOT_GAP - Duration::from_millis(10));
        }
    }

    #[test]
    fn output_over_the_budget_is_dropped_and_a_snapshot_follows_once_the_budget_is_back() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, &vec![b's'; 4096]))).unwrap();
        tx.send((OUTPUT, with_offset(4096, &vec![b'a'; 100]))).unwrap();
        tx.send((OUTPUT, with_offset(4196, b"more"))).unwrap();
        let ctl = leased(Duration::from_millis(3000));
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(4200, b"fresh")), (OUTPUT, with_offset(4200, b"after"))],
            asked: vec![] };
        // Empty, refilling 2000 bytes a second: the snapshot overdraws it to -4096 for about 2 s.
        let budget = Budget { tokens: 0.0, cap: 64.0 * 1024.0, per_s: 2000.0, at: Instant::now() };
        let sent = std::cell::RefCell::new(vec![]);
        let started = Instant::now();
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push(f); true }, budget).unwrap();
        let sent = sent.into_inner();
        let shape: Vec<(String, u64)> = sent.iter().map(|f| (f["kind"].as_str().unwrap().into(), f["seq"].as_u64().unwrap())).collect();
        // seq 1 is the dropped output's: the gap sends viewers to the next snapshot; "more" never went.
        assert_eq!(shape, [("snapshot".into(), 0), ("snapshot".into(), 2), ("output".into(), 3)]);
        assert_eq!(holder.asked.len(), 1, "one request, not one a tick");
        assert!(holder.asked[0] - started >= Duration::from_millis(1800), "asked only once the budget was back");
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
    fn a_nudge_is_taken_only_for_a_live_mirror_of_the_same_generation() {
        let sid = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";
        let live = |s: &str| (s == sid).then(|| "gen-1".to_owned());
        let frame = |s: &str, g: &str| json!({"type": "local.session.watch", "sessionId": s, "generation": g});
        assert_eq!(nudged(&frame(sid, "gen-1"), live).as_deref(), Some(sid));
        assert_eq!(nudged(&frame(sid, "gen-0"), live), None, "another generation");
        assert_eq!(nudged(&frame("bbbbcccc-dddd-4eee-8fff-000011112222", "gen-1"), live), None, "not this Mac's");
        assert_eq!(nudged(&json!({"type": "local.session.watch"}), live), None);
        // An ended mirror is never in LIVE (`index` skips it), so it is "not this Mac's".
    }

    #[test]
    fn a_nudged_relay_survives_an_ack_without_it_inside_the_grace_and_stops_after() {
        let sid = "aaaabbbb-cccc-4ddd-8eee-ffff00001111".to_owned();
        let spawned = std::cell::RefCell::new(vec![]);
        let fake = |sid: String, since: u64| { spawned.borrow_mut().push((sid, since)); Arc::new(Ctl::new(since)) };
        let mut relays = BTreeMap::new();
        nudge_relay(&mut relays, sid.clone(), &fake);
        assert_eq!(*spawned.borrow(), [(sid.clone(), u64::MAX)], "started at once, before any ack");
        settle(&mut relays, &BTreeMap::new(), &fake);
        assert!(relays.contains_key(&sid), "an ack built before the viewer arrived does not stop it");
        // The first ack that lists it renews it without a second snapshot.
        settle(&mut relays, &BTreeMap::from([(sid.clone(), 1_760_000_000)]), &fake);
        assert!(!relays[&sid].snapshot.load(SeqCst));
        assert_eq!(relays[&sid].since.load(SeqCst), 1_760_000_000);
        *relays[&sid].keep.lock().unwrap() = Instant::now();
        let ctl = relays[&sid].clone();
        settle(&mut relays, &BTreeMap::new(), &fake);
        assert!(!relays.contains_key(&sid) && ctl.stop.load(SeqCst), "after the grace the ack rules again");
        assert_eq!(spawned.borrow().len(), 1);
        // A nudge for a running relay asks it for a fresh screen instead of starting another.
        let mut relays = BTreeMap::from([(sid.clone(), Arc::new(Ctl::new(5)))]);
        nudge_relay(&mut relays, sid.clone(), &fake);
        assert!(relays[&sid].snapshot.load(SeqCst) && relays[&sid].kept());
        assert_eq!(spawned.borrow().len(), 1);
    }

    #[test]
    fn a_nudge_streams_the_holders_snapshot_before_any_ack() {
        let sid = "aaaabbbb-cccc-4ddd-8eee-ffff00001111".to_owned();
        let (mac, mut holder) = UnixStream::pair().unwrap();
        // A held session's holder: answers the watch HELLO, snapshots the new client unasked, exits.
        let fake_holder = std::thread::spawn(move || {
            let (kind, hello) = read_frame(&mut holder).unwrap();
            assert_eq!((kind, hello.as_slice()), (HELLO, br#"{"role":"watch"}"#.as_slice()));
            holder.write_all(&encode(HELLO, br#"{"cols":100,"rows":30}"#)).unwrap();
            holder.write_all(&encode(SNAPSHOT, &with_offset(0, b"\x1b[Hscreen"))).unwrap();
            holder.write_all(&encode(EXIT, b"{}")).unwrap();
        });
        let sent = Arc::new(Mutex::new(vec![]));
        let out = sent.clone();
        // `spawn` as `run` uses it, minus the holder lookup: the relay thread attaches at once.
        let attach = |sid: String, since: u64| {
            let ctl = Arc::new(Ctl::new(since));
            let (theirs, out) = (ctl.clone(), out.clone());
            let mut mac = mac.try_clone().unwrap();
            std::thread::spawn(move || {
                watch(&mut mac, &sid, "gen-1", &theirs, &|f| { out.lock().unwrap().push(f); true }).unwrap();
            });
            ctl
        };
        let mut relays = BTreeMap::new();
        nudge_relay(&mut relays, sid.clone(), attach); // no `offer`, no ack
        fake_holder.join().unwrap();
        let started = Instant::now();
        while sent.lock().unwrap().is_empty() && started.elapsed() < Duration::from_secs(2) {
            std::thread::yield_now();
        }
        let first = sent.lock().unwrap().first().cloned().expect("a screen frame before any ack");
        assert_eq!((first["type"].as_str(), first["kind"].as_str(), first["seq"].as_u64()),
            (Some("local.session.screen"), Some("snapshot"), Some(0)));
        assert_eq!((first["sessionId"].as_str(), first["cols"].as_u64()), (Some(sid.as_str()), Some(100)));
        let _ = mac.shutdown(std::net::Shutdown::Both);
    }

    fn attach(id: &str, job: &str, pid: u32) -> HolderInfo {
        HolderInfo { id: id.into(), socket: format!("/x/{id}.sock").into(), pid, provider_pid: pid + 1,
            argv: manager::attach_argv(std::path::Path::new("/c/claude"), job), cwd: "/".into(), watch: true, type_input: true }
    }

    fn pids(holders: Vec<HolderInfo>) -> Vec<u32> {
        holders.iter().map(|h| h.pid).collect()
    }

    #[test]
    fn an_attach_terminal_nobody_uses_is_closed_and_only_then() {
        let held = HolderInfo { argv: vec!["/c/claude".into()], ..attach("00000000", "00000000", 9) };
        let holders = [attach("aaaaaaaa", "11111111", 10), attach("bbbbbbbb", "22222222", 20), attach("cccccccc", "33333333", 30), held];
        let watched = BTreeSet::from(["22222222".to_owned()]);
        let running = BTreeSet::from(["11111111".to_owned(), "22222222".to_owned(), "33333333".to_owned()]);
        let mut wanted = BTreeMap::new();
        let t0 = Instant::now();
        assert!(idle_attach(&holders, Some("cccccccc"), &watched, Some(&running), &mut wanted, t0).is_empty(), "first sight is a fresh start");
        assert_eq!(
            pids(idle_attach(&holders, Some("cccccccc"), &watched, Some(&running), &mut wanted, t0 + IDLE)),
            [10],
            "not the panel's, not a watched one, never a held session's holder"
        );
    }

    #[test]
    fn an_attach_terminal_waits_idle_from_when_the_panel_stopped_showing_it() {
        let holders = [attach("aaaaaaaa", "11111111", 10), attach("bbbbbbbb", "22222222", 20)];
        let running = BTreeSet::from(["11111111".to_owned(), "22222222".to_owned()]);
        let mut wanted = BTreeMap::new();
        let t0 = Instant::now();
        let none = BTreeSet::new();
        assert!(idle_attach(&holders, Some("aaaaaaaa"), &none, Some(&running), &mut wanted, t0).is_empty());
        let watched = BTreeSet::from(["22222222".to_owned()]);
        let hide = t0 + IDLE * 5;
        assert!(idle_attach(&holders, Some("aaaaaaaa"), &watched, Some(&running), &mut wanted, hide).is_empty(),
            "shown and watched for minutes");
        assert!(idle_attach(&holders, None, &none, Some(&running), &mut wanted, hide + IDLE / 2).is_empty(),
            "the panel just closed and the relay just stopped: not reaped at once");
        assert_eq!(pids(idle_attach(&holders, None, &none, Some(&running), &mut wanted, hide + IDLE)), [10, 20]);
    }

    #[test]
    fn an_attach_terminal_whose_job_ended_is_closed_even_when_shown_or_watched() {
        let holders = [attach("bbbbbbbb", "22222222", 20), attach("cccccccc", "33333333", 30)];
        let watched = BTreeSet::from(["22222222".to_owned()]);
        let mut wanted = BTreeMap::new();
        let t0 = Instant::now();
        let running = BTreeSet::from(["33333333".to_owned()]);
        assert_eq!(pids(idle_attach(&holders, Some("cccccccc"), &watched, Some(&running), &mut wanted, t0)), [20],
            "a watched one whose job is no longer listed goes at once");
        assert_eq!(pids(idle_attach(&holders, Some("bbbbbbbb"), &watched, Some(&BTreeSet::new()), &mut wanted, t0)), [20, 30],
            "the panel's too");
        assert!(idle_attach(&holders, Some("cccccccc"), &watched, None, &mut wanted, t0).is_empty(),
            "a failed listing is no evidence that a job ended");
    }

    #[test]
    fn an_attach_terminal_stays_while_its_job_is_listed_even_when_its_capture_failed() {
        // A row capture cannot use (no pid) still names its running job.
        let rows = [serde_json::json!({"kind":"background","id":"22222222","sessionId":"x","status":"busy"})];
        assert!(rows.iter().all(|r| r["pid"].is_null()), "this row fails capture");
        let (_, running) = super::super::claude::listed(&rows);
        let holders = [attach("bbbbbbbb", "22222222", 20)];
        let watched = BTreeSet::from(["22222222".to_owned()]);
        let mut wanted = BTreeMap::new();
        assert!(idle_attach(&holders, None, &watched, Some(&running), &mut wanted, Instant::now()).is_empty(),
            "a watched terminal whose job is listed is kept");
    }

    #[test]
    fn an_attach_terminal_is_started_once_and_never_in_a_loop() {
        let made = attach("dddddddd", "44444444", 40);
        let calls = std::cell::Cell::new(0);
        let start = |_: &str| { calls.set(calls.get() + 1); Some(Ok(made.clone())) };
        assert_eq!(attach_holder("44444444", Vec::new, &start), Some(made.clone()));
        assert_eq!(attach_holder("44444444", Vec::new, &start), None, "its client exited at once: wait, do not restart");
        assert_eq!(attach_holder("44444444", || vec![made.clone()], &start), Some(made.clone()), "a running one is reused");
        assert_eq!(calls.get(), 1);
        // A caller that may not start one (a viewer, size unknown) spends no restart gap.
        assert_eq!(attach_holder("55555555", Vec::new, |_| None), None);
        assert!(attach_holder("55555555", Vec::new, |_| Some(Ok(made.clone()))).is_some(), "the panel starts it right after");
    }

    #[test]
    fn an_attach_terminal_starts_at_the_jobs_size_and_only_the_panel_starts_one_blind() {
        assert_eq!(start_size(Some((100, 30)), false), Some(Some((100, 30))), "a viewer attaches at the job's current size");
        assert_eq!(start_size(Some((100, 30)), true), Some(Some((100, 30))));
        assert_eq!(start_size(None, true), Some(None), "the panel sizes it itself");
        assert_eq!(start_size(None, false), None, "a viewer never resizes the owner's terminal");
        assert!(tty_size(std::process::id()).is_none_or(|(c, r)| c > 0 && r > 0));
        assert_eq!(tty_size(u32::MAX), None, "no such process");
    }

    #[test]
    fn a_session_maps_to_the_holder_discovery_named() {
        let holder = |id: &str| HolderInfo {
            id: id.into(), socket: std::path::PathBuf::from(format!("/x/{id}.sock")), pid: 1, provider_pid: 2,
            argv: vec!["/claude".into()], cwd: "/w".into(), watch: true, type_input: true };
        assert_eq!(matched(vec![holder("aaaa0001"), holder("aaaa0002")], "aaaa0002").unwrap().id, "aaaa0002");
        assert!(matched(vec![holder("aaaa0001")], "aaaa0003").is_none(), "a gone holder has no terminal");
    }
}
