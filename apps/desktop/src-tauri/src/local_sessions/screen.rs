//! The live terminal relay (spec §5.2, §5.5; rulings 2026-10-03 §4). While the backend's heartbeat
//! ack lists a held session as watched, attach to its holder as a `watch` client and send
//! `local.session.screen` frames on the relay socket, each with the terminal's size: what is above
//! the holder's screen as `history` frames before the snapshot of a new or grown viewer set; the
//! holder's snapshot; then every 50 ms (`TICK`) only what changed on the screen since (`Relayed`): the
//! rows that scrolled off the top, pushed into the viewers' scrollback in order, then the changed
//! cells, never a clear (so a viewer never sees an erase before its rewrite). A whole snapshot goes
//! again only for a new viewer, after a resize and after a lost frame (at most one a second); one
//! that does not fit a frame (11 KiB raw: base64 plus envelope fits the 16 KiB relay frame) is sent
//! in parts, its first frame saying how many (`parts`). A quiet screen sends a `ping` every 10 s.
//! Nothing is sent while nobody watches. A tick with no room in the relay queue or the backend's
//! budget (2 MiB raw a minute) waits and merges into the next one; output already handed to `Out`
//! waits there and goes out in order (`Out::flush`). Only more than BACKLOG of it waiting is
//! dropped, and a snapshot follows.
//! A `local.session.watch` nudge from the backend starts a relay at once (a new viewer sees the
//! screen in about a second, not at the next ack); the acks still keep it going and stop it.
//! A Claude background session's terminal comes from an attach holder this module starts on demand
//! and stops when nobody uses it, or when discovery no longer lists its job as running.

use super::transport::relay_log;
use super::{Mirror, NativeSession};
use crate::holder::manager::{self, HolderInfo};
use crate::holder::model::Model;
use crate::holder::protocol::{encode, read_frame, split_offset, with_offset, EXIT, HELLO, HISTORY, OUTPUT, RESIZE, SNAPSHOT};
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
/// A quiet relay pings this often.
const PING_EVERY: Duration = Duration::from_secs(10);
/// A relay no heartbeat ack has renewed for this long stops (its relay socket is down).
const LEASE: Duration = Duration::from_secs(30);
const TICK: Duration = Duration::from_millis(50);
/// How long a new relay waits for a session that just ended to be indexed with its kept screen.
const SETTLE_WAIT: Duration = Duration::from_secs(5);
/// How long output is held back from the point where the provider leaves the alternate screen.
/// Claude Code leaves it to shut down and prints "Resume this session with: ..." on the normal
/// screen; it exited 0.47 s after (2.1.292, `holder::model::CLAUDE_EXIT_READS`). Those lines are not
/// the session's picture. A program that left mid-session (an editor closing) is this much late.
// ponytail: a provider slower than this to exit shows its exit lines until the kept snapshot
// replaces them; the holder's own rule (`model::SHUTDOWN_LEAVE`, 5 s) is the upper bound to move to.
const LEAVE_HOLD: Duration = Duration::from_secs(2);
/// A nudged relay outlives acks that miss it for this long: an ack the backend built just before
/// the viewer arrived can land after the nudge. Longer than one 10 s heartbeat.
const GRACE: Duration = Duration::from_secs(15);
/// Snapshot requests are at most this often: a lost frame on a slow uplink must not become a
/// full screen every tick.
const SNAPSHOT_GAP: Duration = Duration::from_secs(1);
/// The backend's per-session budget (`session_screen.BUDGET_BYTES_PER_MINUTE`).
const BUDGET_PER_MINUTE: usize = 2 * 1024 * 1024;
const BURST: usize = 256 * 1024;
/// Budget a history burst leaves for the snapshot and output after it.
const HISTORY_RESERVE: usize = 64 * 1024;
/// How long a history burst may wait for room in the shallow relay queue before it counts as lost.
const PACE: Duration = Duration::from_secs(3);
/// How long the holder has to answer a HISTORY asked on the relay's own connection (as long as
/// `manager::history` waits on its own): past it the viewers get the screen without the lines.
const LINES_WAIT: Duration = Duration::from_secs(2);
/// Output that may wait here for room in the relay queue or the budget (`Out::flush`). At the
/// budget's rate this much is under 4 s late; past it the viewers are too far behind a live
/// screen, so it is dropped and they get the screen as it is (their scrollback loses those lines
/// until they open the session again). Prod 2026-10-08: the connection loop stalled 6.1 s on an
/// upload while Codex wrote 4 blocks (about 9 KiB), which the full queue lost; 82 rows never
/// reached a viewer's scrollback.
const BACKLOG: usize = 128 * 1024;

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
    pub(super) typist: Mutex<Option<std::sync::mpsc::SyncSender<super::remote_input::Typed>>>,
    snapshot: AtomicBool,
    /// The viewer set grew (an ack's `since` moved, or a nudge): history, then a snapshot.
    history: AtomicBool,
    /// How often a quiet relay pings (PING_EVERY; tests shorten it).
    beat: Duration,
    /// LEAVE_HOLD; tests shorten it.
    leave_hold: Duration,
    pub(super) stop: AtomicBool,
    until: Mutex<Instant>,
    /// Absence from `watching` does not stop the relay before this (a nudge sets it, and the
    /// provider's exit: the backend lists no viewer of an ended session until its kept screen is reported).
    keep: Mutex<Instant>,
    /// The holder this relay reads, once `run` has found it.
    holder: Mutex<Option<String>>,
    /// That holder keeps the lines above its screen (HELLO `"history": true`): they are asked for
    /// on the relay's own connection, where their place among the output is known (`pump`).
    lines: AtomicBool,
}

impl Ctl {
    pub(super) fn new(since: u64) -> Self {
        Self { since: AtomicU64::new(since), epoch: AtomicU64::new(0), typist: Mutex::new(None), snapshot: AtomicBool::new(false),
            history: AtomicBool::new(false), beat: PING_EVERY, leave_hold: LEAVE_HOLD, stop: AtomicBool::new(false),
            until: Mutex::new(Instant::now() + LEASE), keep: Mutex::new(Instant::now()), holder: Mutex::new(None), lines: AtomicBool::new(false) }
    }
    fn renew(&self) {
        if let Ok(mut until) = self.until.lock() {
            *until = Instant::now() + LEASE;
        }
    }
    fn nudged(&self) {
        self.renew();
        self.hold();
    }
    /// Acks that do not list this relay's session do not stop it for GRACE.
    fn hold(&self) {
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
        .iter()
        .filter(|(key, m)| m.ended_at.is_none() && !super::transport::stopped(key, m))
        .filter_map(|(_, m)| Some((m.session_id.clone()?, Live { generation: m.generation.clone(), native: m.native.clone() })))
        .collect();
    if let Ok(mut slot) = LIVE.lock() {
        *slot = live;
    }
    INDEXED.store(true, SeqCst);
}

/// Sharing stopped for this session: its relay stops sending now, and no watch, typed input or
/// panel finds it live. `index` keeps it out until it is shared again.
pub(super) fn revoke(session_id: &str) {
    if let Ok(mut live) = LIVE.lock() {
        live.remove(session_id);
    }
    if let Some(ctl) = RELAYS.lock().ok().and_then(|mut relays| relays.remove(session_id)) {
        ctl.stop.store(true, SeqCst);
    }
}

/// Whether `index` has run since this app started. Until then `LIVE` says nothing.
static INDEXED: AtomicBool = AtomicBool::new(false);

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
            // A Codex client draws at its own size, so anyone may start one; a Claude job draws at
            // the latest attach size, so only its own size or the owner's panel may.
            let codex = manager::attach_provider(job) == Some("codex");
            let holder = attach_holder(job, manager::list, |job| {
                if codex { Some(attach_start(job, None)) } else { start_size(tty_size(pid), panel).map(|size| attach_start(job, size)) }
            })?;
            if let Ok(mut wanted) = WANTED.lock() {
                wanted.insert(holder.id.clone(), Instant::now());
            }
            holder
        }
        _ => return None,
    };
    Some((holder, live.generation))
}

/// The holder that hosts the session's terminal, whose interactive client is the owner's: every
/// key the owner types goes through it. Never a background job's attach holder (the owner's own
/// `claude attach` bypasses that one) and never starts one.
pub(crate) fn hosting_holder(session_id: &str) -> Option<HolderInfo> {
    let id = LIVE.lock().ok()?.get(session_id)?.native.holder.clone()?;
    matched(manager::list(), &id)
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

/// Resume of a background job its daemon retired (`bg retire …: idle-prompt`): `claude attach`
/// starts its process again, under the same job and conversation, and shows it. A running attach
/// holder already does both.
pub(super) fn reopen(job: &str) -> Result<(), String> {
    let mut started = STARTED.lock().map_err(|_| "attach state unavailable")?;
    if manager::list().iter().any(|h| manager::attach_job(h) == Some(job)) {
        return Ok(());
    }
    attach_start(job, None)?;
    started.insert(job.to_owned(), Instant::now());
    Ok(())
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
    let provider = manager::attach_provider(job).ok_or("not an attachable session")?;
    let program = super::provider_program(&home, provider).ok_or_else(|| format!("{provider} is not installed"))?;
    manager::start_sized(&home, &manager::attach_argv(&program, job), size)
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

/// The Codex threads that still run: the ones a live mirror names (discovery saw Codex's daemon
/// hold the rollout). Before the relay loop has published its mirrors once (`indexed`: an app
/// relaunch with attach holders still open) there is no evidence, so each open holder's own
/// thread counts.
fn codex_running<'a>(indexed: bool, live: impl Iterator<Item = &'a str>, holders: &[HolderInfo]) -> BTreeSet<String> {
    if indexed {
        return live.map(str::to_owned).collect();
    }
    holders.iter().filter_map(manager::attach_job).filter(|job| manager::attach_provider(job) == Some("codex")).map(str::to_owned).collect()
}

/// Every job that still runs: Claude's own listing (`claude`, None when it failed: no evidence for
/// anything) plus the Codex threads.
fn running_jobs(claude: Option<&BTreeSet<String>>, codex: BTreeSet<String>) -> Option<BTreeSet<String>> {
    claude.map(|claude| claude.iter().cloned().chain(codex).collect())
}

/// Close attach holders nobody uses or whose job is no longer running. Ends only the attach
/// client; the session runs on under its provider's own daemon. Never `claude stop`.
/// `running`: the Claude background jobs still listed.
pub(super) fn reap_attach(holders: &[HolderInfo], running: Option<&BTreeSet<String>>) {
    let shown = crate::holder::shown::shown_holder();
    let live = LIVE.lock().map(|l| l.clone()).unwrap_or_default();
    let codex = live.values().filter(|l| l.native.provider == "codex").filter_map(|l| l.native.job.as_deref());
    let running = running_jobs(running, codex_running(INDEXED.load(SeqCst), codex, holders));
    let running = running.as_ref();
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
            ctl.history.store(true, SeqCst);
        }
    }
    for (sid, ctl) in relays.iter() {
        if let Some(since) = wanted.get(sid) {
            // Only ever raised: an ack built before a nudge lists an older `since`, and taking it
            // would make the next ack look like one more new viewer (a second history burst).
            // MAX is "not known yet" (a nudge from a backend that sends no `since`).
            let _ = ctl.since.fetch_update(SeqCst, SeqCst, |was| Some(if was == u64::MAX { *since } else { was.max(*since) }));
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
    #[cfg(test)]
    if let (Ok(mut nudged), Some(sid)) = (NUDGED.lock(), body["sessionId"].as_str()) {
        nudged.insert(sid.to_owned());
    }
    let Some(sid) = LIVE.lock().ok().and_then(|live| nudged(body, |sid| live.get(sid).map(|l| l.generation.clone())))
    else { return };
    let Ok(mut relays) = RELAYS.lock() else { return };
    relays.retain(|_, ctl| !ctl.stopped());
    nudge_relay(&mut relays, sid, body["since"].as_u64(), spawn);
}

/// Every session id a `local.session.watch` named, as the relay loop handed it over. (Not
/// `RELAYS`: `LIVE` is one static, and parallel tests' connections index over each other.)
#[cfg(test)]
static NUDGED: Mutex<std::collections::BTreeSet<String>> = Mutex::new(std::collections::BTreeSet::new());

#[cfg(test)]
pub(super) fn was_nudged(sid: &str) -> bool {
    NUDGED.lock().is_ok_and(|nudged| nudged.contains(sid))
}

/// Someone watches this session in Lightbulb now: the last heartbeat ack (`offer`) or a
/// `local.session.watch` (`nudge`) runs its relay.
pub(super) fn watched(sid: &str) -> bool {
    RELAYS.lock().is_ok_and(|relays| relays.get(sid).is_some_and(|ctl| !ctl.stopped()))
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

/// `since`: the viewer's arrival as the next ack will list it (the nudge carries it), so that ack
/// is not taken for one more new viewer. None from a backend that does not send it.
fn nudge_relay(relays: &mut BTreeMap<String, Arc<Ctl>>, sid: String, since: Option<u64>, spawn: impl Fn(String, u64) -> Arc<Ctl>) {
    if let Some(ctl) = relays.get(&sid) {
        // Running already: the new viewer needs history and a screen now, not in up to 10 s.
        ctl.history.store(true, SeqCst);
        if let Some(since) = since {
            let _ = ctl.since.fetch_update(SeqCst, SeqCst, |was| Some(if was == u64::MAX { since } else { was.max(since) }));
        }
        ctl.nudged();
        return;
    }
    // The holder snapshots a new watch client unasked. With no `since`, MAX keeps the first ack
    // that lists it from asking for a second snapshot.
    let ctl = spawn(sid.clone(), since.unwrap_or(u64::MAX));
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
    let mut found = holder_for_session(sid, false);
    // A held session that just ended: its holder keeps the final screen, and the index names it
    // once the holder has said so (`session_states`). Until then it is not "not held".
    let asked = Instant::now();
    while found.is_none() && asked.elapsed() < SETTLE_WAIT && !ctl.stopped() && super::session_states::is_settling(sid) {
        std::thread::sleep(Duration::from_millis(100));
        found = holder_for_session(sid, false);
    }
    let (holder, generation) = found.ok_or("not a held session")?;
    if !holder.watch {
        return Err("its holder predates live terminals; restart the session to share its screen".into());
    }
    let mut conn = UnixStream::connect(&holder.socket).map_err(|e| e.to_string())?;
    if let Ok(mut reads) = ctl.holder.lock() {
        *reads = Some(holder.id.clone());
    }
    let socket = holder.socket.clone();
    let history = move |max: usize| manager::history(&socket, max);
    let result = watch(&mut conn, sid, &generation, ctl, &send_out, &history);
    // Every exit, early ones included: the reader thread's clone is unblocked only by this.
    let _ = conn.shutdown(std::net::Shutdown::Both);
    result
}

fn watch(conn: &mut UnixStream, sid: &str, generation: &str, ctl: &Ctl, send: &dyn Fn(Value) -> bool, history: &dyn Fn(usize) -> Option<Vec<u8>>) -> Result<(), String> {
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
    ctl.lines.store(facts["history"] == true, SeqCst);
    pump(&frames, conn, (sid, generation), size, ctl, send, Budget::new(), history)
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
    /// Bytes a history burst may use now: what is left above the reserve, at most BURST.
    fn room(&mut self) -> usize {
        (self.left() - HISTORY_RESERVE as f64).clamp(0.0, BURST as f64) as usize
    }
}

/// The session's screen as the holder draws it (`model`) and as every viewer has it (`shown`).
/// The diff between them is what a tick sends: never a clear, so a viewer never sees an erase
/// without its rewrite (the flash the raw byte stream showed, 2026-10-11).
struct Relayed {
    model: Model,
    shown: vt100::Parser,
    fed: bool,
    /// Since when the provider is inside a synchronized update (DEC mode 2026: `ESC[?2026h` ...
    /// `ESC[?2026l`, Codex draws with it): its screen is half drawn until the update ends.
    sync: Option<Instant>,
    /// The last bytes fed, for a mode 2026 sequence split across two reads.
    sync_tail: Vec<u8>,
    /// Since when a tick has waited because it would blank cells the viewers show.
    erasing: Option<Instant>,
}

/// The longest a synchronized update holds the viewers' screen: a provider that never ends one
/// must not freeze them.
const SYNC_CAP: Duration = Duration::from_millis(250);
/// The longest a tick that would blank cells waits for their repaint (a provider without mode
/// 2026 that erases in one read and draws again in the next, 2026-10-11).
const ERASE_CAP: Duration = Duration::from_millis(150);
const SYNC_ON: &[u8] = b"\x1b[?2026h";
const SYNC_OFF: &[u8] = b"\x1b[?2026l";

/// Cells with something in them.
fn filled(screen: &vt100::Screen) -> usize {
    let (rows, cols) = screen.size();
    (0..rows).flat_map(|r| (0..cols).map(move |c| (r, c))).filter(|&(r, c)| screen.cell(r, c).is_some_and(|cell| cell.has_contents())).count()
}

impl Relayed {
    fn seed(screen: &[u8], (cols, rows): (u16, u16)) -> Self {
        let mut model = Model::new(rows, cols);
        model.inline_codex(); // ESC[3J drops the lines above, as it does in the viewers' terminals
        let _ = model.feed(screen);
        let _ = model.scrolled();
        let mut shown = vt100::Parser::new(rows, cols, 0);
        shown.process(screen);
        Self { model, shown, fed: false, sync: None, sync_tail: vec![], erasing: None }
    }
    fn feed(&mut self, bytes: &[u8]) {
        let _ = self.model.feed(bytes);
        self.fed = true;
        // ponytail: only the plain forms; `ESC[?2026;...h` with other modes is not seen (no provider sends one).
        let hay = [self.sync_tail.as_slice(), bytes].concat();
        let last = |needle: &[u8]| hay.windows(needle.len()).rposition(|w| w == needle);
        match (last(SYNC_ON), last(SYNC_OFF)) {
            // (A block opened behind one that closed keeps the clock: back-to-back blocks, as Codex
            // streams them, still tick every SYNC_CAP.)
            (Some(on), off) if off.is_none_or(|off| on > off) => {
                self.sync.get_or_insert_with(Instant::now);
            }
            (_, Some(_)) => self.sync = None,
            _ => {}
        }
        // (Shorter than a whole sequence, so one is never found twice.)
        self.sync_tail = hay[hay.len().saturating_sub(SYNC_ON.len() - 1)..].to_vec();
    }
    /// Whether a tick now shows the viewers a whole picture: not inside a synchronized update, and
    /// not an erase whose repaint may still come (each for its cap at most).
    // ponytail: "an erase" is fewer filled cells than the viewers have, so a scroll that brings in
    // blank rows or a deleted character also waits, ERASE_CAP at most; a per-cell check after the
    // rows are pushed is the upgrade if that delay ever shows.
    fn ready(&mut self, now: Instant) -> bool {
        if !self.fed {
            return true;
        }
        if self.sync.is_some_and(|at| now.duration_since(at) < SYNC_CAP) {
            return false;
        }
        let (model, shown) = (self.model.screen(), self.shown.screen());
        if model.alternate_screen() != shown.alternate_screen() || filled(model) >= filled(shown) {
            self.erasing = None;
            return true;
        }
        now.duration_since(*self.erasing.get_or_insert(now)) >= ERASE_CAP
    }
    /// Bytes that turn every viewer's terminal into the model's: the screen switch, rows that left
    /// the top pushed into their scrollback (written on row 1, then scrolled from the bottom), then
    /// only the changed cells. Empty when nothing was fed.
    fn tick(&mut self) -> Vec<u8> {
        if !std::mem::take(&mut self.fed) {
            return vec![];
        }
        self.erasing = None;
        // A block still open is held again from now: the viewers were just updated.
        if self.sync.is_some() {
            self.sync = Some(Instant::now());
        }
        let mut out = Vec::new();
        let alt = self.model.screen().alternate_screen();
        if alt != self.shown.screen().alternate_screen() {
            out.extend_from_slice(if alt { b"\x1b[?1049h" } else { b"\x1b[?1049l" });
        }
        let (erased, rows) = self.model.scrolled();
        if erased {
            out.extend_from_slice(b"\x1b[3J");
        }
        let height = self.model.size().0;
        // (A row a long line wrapped onto lands in their scrollback as a line of its own: a resize
        // reflows it apart from the line it came from.)
        for row in rows {
            out.extend_from_slice(b"\x1b[r\x1b[m\x1b[1;1H\x1b[2K");
            out.extend_from_slice(&row);
            out.extend_from_slice(format!("\x1b[m\x1b[{height};1H\n").as_bytes());
        }
        self.shown.process(&out);
        let diff = self.model.screen().state_diff(self.shown.screen());
        self.shown.process(&diff);
        out.extend(diff);
        out
    }
}

struct Out<'a> {
    ids: (&'a str, &'a str),
    epoch: u64,
    seq: u64,
    ctl: &'a Ctl,
    send: &'a dyn Fn(Value) -> bool,
    budget: Budget,
    /// Output not sent yet, oldest first, each run with the terminal size it was written at.
    backlog: std::collections::VecDeque<((u16, u16), Vec<u8>)>,
    /// A frame was lost since the last snapshot: viewers wait for the next one, so output is waste.
    stale: bool,
}

impl Out<'_> {
    /// Output waits for its turn (`flush`), so no viewer misses any of it: a missing frame is
    /// repaired with a snapshot, which is the screen only, and the lines that output scrolled off
    /// the top would never reach a viewer's scrollback. A snapshot goes when no output waits (it is
    /// the screen after that output) and may overdraw the budget: it ends a gap, and the next
    /// request waits for the budget to be back above zero. One that cannot go is asked for again,
    /// and false says so (the viewers' screen is not that snapshot).
    fn emit(&mut self, kind: &str, bytes: &[u8], size: (u16, u16)) -> bool {
        if kind != "snapshot" {
            // (After a loss viewers wait for the snapshot: output until then is in it.)
            if !self.stale && !bytes.is_empty() {
                match self.backlog.back_mut() {
                    Some((at, held)) if *at == size => held.extend_from_slice(bytes),
                    _ => self.backlog.push_back((size, bytes.to_vec())),
                }
                self.flush();
            }
            return true;
        }
        self.flush();
        if !self.backlog.is_empty() {
            self.ctl.snapshot.store(true, SeqCst);
            return false;
        }
        let mut seq = self.seq;
        for (i, frame) in frames_for(self.ids, self.epoch, &mut seq, size, kind, bytes).into_iter().enumerate() {
            if (self.send)(frame) {
                self.seq += 1;
                continue;
            }
            if i == 0 {
                // None of it went: no place in the order is taken, and viewers miss nothing.
                self.ctl.snapshot.store(true, SeqCst);
                return false;
            }
            // The rest of a snapshot is output after its first frame: it waits like any other.
            self.backlog.push_back((size, bytes[i * CHUNK..].to_vec()));
            break;
        }
        let waits = self.backlog.front().map_or(0, |(_, held)| held.len());
        self.budget.take(bytes.len() - waits, true);
        self.stale = false;
        true
    }
    /// Sends the waiting output in order, as far as the budget and the relay queue take it now. A
    /// chunk that does not go keeps its place and takes no seq, so viewers see no gap: the output is
    /// late, never missing. More than BACKLOG waiting is given up: one skipped seq tells viewers a
    /// frame is lost, and a snapshot follows.
    fn flush(&mut self) {
        while let Some((size, held)) = self.backlog.front_mut() {
            let len = held.len().min(CHUNK);
            if self.budget.left() < len as f64 {
                break;
            }
            let mut seq = self.seq;
            let frame = frames_for(self.ids, self.epoch, &mut seq, *size, "output", &held[..len]).remove(0);
            if !(self.send)(frame) {
                break;
            }
            self.budget.take(len, true);
            self.seq = seq;
            held.drain(..len);
            if held.is_empty() {
                self.backlog.pop_front();
            }
        }
        if self.backlog.iter().map(|(_, held)| held.len()).sum::<usize>() > BACKLOG {
            self.give_up();
        }
    }
    /// The waiting output will not be sent: one skipped seq tells viewers a frame is lost, and a
    /// snapshot follows.
    fn give_up(&mut self) {
        self.backlog.clear();
        self.seq += 1;
        self.lost();
    }
    /// Waits for the waiting output to go, for PACE at most (the limit a history burst already has
    /// for the same queue); what has not gone by then is given up. False then.
    fn drained(&mut self) -> bool {
        let deadline = Instant::now() + PACE;
        loop {
            self.flush();
            if self.backlog.is_empty() {
                return !self.stale;
            }
            if Instant::now() >= deadline {
                self.give_up();
                return false;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
    /// What is above the holder's screen, for viewers with none of it yet (spec §5.2), sized to
    /// what the budget allows now so the snapshot after it always fits: a short budget trims it
    /// from its old end. Paced into the shallow relay queue (a burst is up to 24 frames); a frame
    /// that finds no room within PACE is lost like any other. Nothing above the screen is one
    /// empty frame: a viewer then knows it has all there is, and no later burst is for it.
    ///
    /// Never ahead of output that waits here (review of #3912): the holder's lines and the snapshot
    /// behind them are the terminal after that output, so sent before it, the output would draw
    /// over them again, in every viewer. The burst waits for it (`drained`, PACE at most, so a
    /// stuck queue does not hold a new viewer for long). False when it must be asked for again:
    /// the output was given up, or was before, and the snapshot that repairs that goes first.
    fn history(&mut self, fetch: &dyn Fn(usize) -> Option<Vec<u8>>, size: (u16, u16)) -> bool {
        if self.stale || !self.drained() {
            return false;
        }
        if let Some(bytes) = self.fetched(fetch) {
            self.burst(&bytes, size);
        }
        true
    }
    /// The same burst from lines the holder sent on the relay's own connection (`pump`): `bytes`,
    /// without its oldest lines when the budget allows less now than when they were asked for
    /// (`newest_lines`). False as for `history`.
    fn lines(&mut self, bytes: &[u8], size: (u16, u16)) -> bool {
        if self.stale || !self.drained() {
            return false;
        }
        // (When not one line fits, nothing is sent: an empty burst says "there are none", and the
        // line feeds that end the holder's answer alone would be blank rows.)
        let fits = newest_lines(bytes, self.budget.room());
        if fits.contains(&b'\r') || bytes.is_empty() {
            self.burst(fits, size);
        }
        true
    }
    /// The session ended on a picture that shows again lines its viewers have above their screens
    /// (the holder's EXIT says `startOver`, `Model::end`): a new run, so every viewer's terminal
    /// starts over from the lines above that picture, as one that opens the session later does.
    /// Only with all of those lines in hand (`whole` bytes, as the holder counted them): a viewer
    /// who watched for long has more above its screen than a burst the budget trimmed, or none,
    /// would give back, and the reset behind a new run would cost it the rest. Without them the
    /// run goes on: the picture is drawn over the screen and those viewers keep their own lines,
    /// with the ones the picture shows again there twice.
    fn start_over(&mut self, fetch: &dyn Fn(usize) -> Option<Vec<u8>>, size: (u16, u16), whole: usize) {
        if whole > self.budget.room() {
            return;
        }
        let Some(bytes) = fetch(whole).filter(|bytes| bytes.len() == whole) else { return };
        self.epoch = next_epoch(&LAST_EPOCH, SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64));
        self.ctl.epoch.store(self.epoch, SeqCst);
        self.seq = 0;
        self.stale = false;
        // Output still waiting is the old run's, and the lines above the kept picture are the
        // terminal after it: it is dropped, not sent behind them. The new run draws everything,
        // and a viewer drops an older run's frames anyway.
        self.backlog.clear();
        self.burst(&bytes, size);
    }
    fn fetched(&mut self, fetch: &dyn Fn(usize) -> Option<Vec<u8>>) -> Option<Vec<u8>> {
        let room = self.budget.room();
        let bytes = (room > 0).then(|| fetch(room)).flatten()?;
        Some(bytes[bytes.len().saturating_sub(room)..].to_vec())
    }
    fn burst(&mut self, bytes: &[u8], size: (u16, u16)) {
        self.budget.take(bytes.len(), true);
        let deadline = Instant::now() + PACE;
        for frame in frames_for(self.ids, self.epoch, &mut self.seq, size, "history", bytes) {
            while !(self.send)(frame.clone()) {
                if Instant::now() >= deadline {
                    self.lost();
                    return;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }
    /// Proof of life on a quiet screen: no data, and no place in the order (it carries the next
    /// seq without taking it), so a lost one costs nothing.
    fn ping(&mut self, size: (u16, u16)) {
        let mut seq = self.seq;
        for frame in frames_for(self.ids, self.epoch, &mut seq, size, "ping", b"") {
            let _ = (self.send)(frame);
        }
    }
    fn lost(&mut self) {
        // Viewers skip to the next snapshot after a missing frame: ask for one.
        self.stale = true;
        self.ctl.snapshot.store(true, SeqCst);
    }
}

/// The newest whole lines of the holder's `lines` (`Model::history`) that fit `room` bytes. Output
/// sent between the request and the answer can leave less room than was asked for (review of
/// #3912): a cut by bytes then began inside a character or an escape sequence, and the viewer's
/// first line was garbage. Each line there is a row drawn from the default attributes that ends
/// with a reset and CR LF, so what follows any CR LF starts clean in an empty terminal.
fn newest_lines(lines: &[u8], room: usize) -> &[u8] {
    let cut = lines.len().saturating_sub(room);
    let from = (cut..lines.len()).find(|at| *at == 0 || lines[..*at].ends_with(b"\r\n")).unwrap_or(lines.len());
    &lines[from..]
}

static LAST_EPOCH: AtomicU64 = AtomicU64::new(0);

/// Wall-clock milliseconds, but never equal to or below an earlier epoch: a viewer drops a lower one as stale,
/// and a clock step back or two relays in one millisecond would otherwise freeze it.
fn next_epoch(last: &AtomicU64, now_ms: u64) -> u64 {
    let prev = last.fetch_update(SeqCst, SeqCst, |l| Some(now_ms.max(l + 1))).unwrap_or(0);
    now_ms.max(prev + 1)
}

/// Holder frames in, relay frames out, until the lease ends or the holder goes. A session's exit
/// does not end it: a holder that stays keeps the final screen, and this relay shows it.
#[allow(clippy::too_many_arguments)]
fn pump(frames: &Receiver<(u8, Vec<u8>)>, to_holder: &mut impl Write, ids: (&str, &str), mut size: (u16, u16),
        ctl: &Ctl, send: &dyn Fn(Value) -> bool, budget: Budget, history: &dyn Fn(usize) -> Option<Vec<u8>>) -> Result<(), String> {
    let epoch = next_epoch(&LAST_EPOCH, SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64));
    ctl.epoch.store(epoch, SeqCst);
    let mut out = Out { ids, epoch, seq: 0, ctl, send, budget, backlog: Default::default(), stale: false };
    // A holder that keeps its lines is asked for them on this connection (review of #3912). It is one
    // event loop and answers in order, with the screen right behind the lines: every OUTPUT that
    // reaches this relay before that answer is of bytes the lines and the screen already show, and
    // is sent before them; every one after is of bytes they do not show, and is sent after. Asked
    // on another connection (`manager::history`), the lines could hold output this relay had not
    // sent yet, or not read yet: a viewer drawn from them then got those bytes again, or missed the
    // lines that scrolled off between the lines and the snapshot asked for after them.
    let keeps = ctl.lines.load(SeqCst);
    // Since when the holder owes those lines. A snapshot that comes before them is older than
    // they are, and is skipped: the one behind the lines is the picture.
    let mut asked_lines: Option<Instant> = None;
    // A new run's viewers get history first; the holder's own first snapshot is queued behind it.
    ctl.history.store(keeps, SeqCst);
    if !keeps {
        out.history(history, size);
    }
    // The holder's screen and the viewers', from the last snapshot sent: each tick sends the
    // difference. None before the first snapshot, and from a resize or an exit until the next
    // (output meanwhile is in that snapshot).
    let mut relayed: Option<Relayed> = None;
    let mut last_tick = Instant::now();
    let mut last_ping = Instant::now();
    // The holder sends a watch client its first snapshot unasked; a request may follow at once
    // (a resize, a lost frame).
    let mut last_snapshot = Instant::now().checked_sub(SNAPSHOT_GAP).unwrap_or_else(Instant::now);
    // The provider exited. An attach holder closes next; a session's own holder stays.
    let mut exited = false;
    // Since when the provider is off the alternate screen it was on (LEAVE_HOLD): no tick until it
    // exits (what it printed on its way out never goes; the kept snapshot is the picture) or the
    // hold is over (a snapshot then).
    let mut leaving: Option<Instant> = None;
    loop {
        if ctl.stopped() {
            return Ok(());
        }
        out.flush();
        if asked_lines.is_some_and(|at| at.elapsed() >= LINES_WAIT) {
            asked_lines = None;
            ctl.snapshot.store(true, SeqCst);
        }
        // (Not while a leave is held, as for a snapshot.)
        if keeps && leaving.is_none() && ctl.history.swap(false, SeqCst) {
            let room = out.budget.room();
            if out.stale {
                // After the snapshot that repairs the lost output (`Out::history`).
                ctl.history.store(true, SeqCst);
            } else if room == 0 {
                // No budget for lines now: the screen alone, as a short budget always gave.
                ctl.snapshot.store(true, SeqCst);
                last_snapshot = Instant::now().checked_sub(SNAPSHOT_GAP).unwrap_or_else(Instant::now);
            } else {
                match to_holder.write_all(&encode(HISTORY, &with_offset(room as u64, b""))) {
                    Err(_) if exited => return Ok(()),
                    sent => sent.map_err(|e| e.to_string())?,
                }
                asked_lines = Some(Instant::now());
            }
        }
        if !keeps && ctl.history.swap(false, SeqCst) {
            // A holder with no lines of its own: its output log's tail, read on another connection
            // (`manager::history`). What the viewers were not sent yet is older than that tail's end
            // and goes first.
            // ponytail: output this relay has not read yet may be in that tail and is then drawn
            // twice by a new viewer; exact only with the log's offsets. No held Claude Code or
            // Codex session takes this path.
            if leaving.is_none() {
                send_changes(&mut relayed, &mut out, size);
            }
            if out.history(history, size) {
                ctl.snapshot.store(true, SeqCst);
                // At once: these viewers have no screen, and SNAPSHOT_GAP is for frames lost on a slow uplink.
                last_snapshot = Instant::now().checked_sub(SNAPSHOT_GAP).unwrap_or_else(Instant::now);
            } else {
                // After the snapshot that repairs the lost output (`Out::history`).
                ctl.history.store(true, SeqCst);
            }
        }
        // Proof of life on a quiet screen; never a redraw (a lost frame is repaired by the
        // viewer's own request, `SNAPSHOT_GAP`).
        if last_ping.elapsed() >= ctl.beat {
            last_ping = Instant::now();
            out.ping(size);
        }
        // Not while a leave is held: a snapshot taken now would show what is held back.
        if ctl.snapshot.load(SeqCst) && leaving.is_none() && last_snapshot.elapsed() >= SNAPSHOT_GAP && out.budget.left() > 0.0 {
            ctl.snapshot.store(false, SeqCst);
            match to_holder.write_all(&encode(SNAPSHOT, b"")) {
                Err(_) if exited => return Ok(()),
                sent => sent.map_err(|e| e.to_string())?,
            }
            last_snapshot = Instant::now();
        }
        match frames.recv_timeout(TICK) {
            Ok((SNAPSHOT, _)) if asked_lines.is_some() => {}
            Ok((HISTORY, lines)) if asked_lines.is_some() => {
                asked_lines = None;
                // As before a snapshot: what the viewers were not sent yet goes first (the lines and
                // the screen behind them show it already). After a leave of the alternate screen
                // nothing goes: the screen the holder sends right behind its lines is the picture.
                if leaving.take().is_none() {
                    send_changes(&mut relayed, &mut out, size);
                } else {
                    relayed = None;
                }
                if !out.lines(&lines, size) {
                    ctl.history.store(true, SeqCst);
                }
            }
            Ok((SNAPSHOT, payload)) => {
                let (_, screen) = split_offset(&payload).ok_or("bad snapshot")?;
                // A snapshot is only the screen: rows the output before it pushed off the top reach
                // a viewer's scrollback only in a tick, so that goes first (prod 2026-10-08: a
                // snapshot that overtook a Codex block cost a viewer its 19 rows). After a leave of
                // the alternate screen nothing does: the provider's way out, never a viewer's lines.
                if leaving.take().is_none() {
                    send_changes(&mut relayed, &mut out, size);
                }
                if out.emit("snapshot", screen, size) {
                    relayed = Some(Relayed::seed(screen, size));
                }
            }
            Ok((OUTPUT, payload)) => {
                let bytes = split_offset(&payload).ok_or("bad output")?.1;
                if let Some(r) = relayed.as_mut() {
                    let was = r.model.screen().alternate_screen();
                    r.feed(bytes);
                    if was && !r.model.screen().alternate_screen() && leaving.is_none() {
                        leaving = Some(Instant::now());
                    }
                }
            }
            Ok((RESIZE, payload)) if payload.len() == 4 => {
                // What changed goes at the old size; the new size starts from a snapshot.
                if leaving.take().is_none() {
                    send_changes(&mut relayed, &mut out, size);
                }
                relayed = None;
                size = (u16::from_be_bytes([payload[0], payload[1]]), u16::from_be_bytes([payload[2], payload[3]]));
                ctl.snapshot.store(true, SeqCst);
                last_snapshot = Instant::now().checked_sub(SNAPSHOT_GAP).unwrap_or_else(Instant::now);
            }
            Ok((EXIT, payload)) => {
                // After a leave: what the provider printed on its way out (Claude Code: the resume
                // lines) never goes.
                if leaving.take().is_none() {
                    send_changes(&mut relayed, &mut out, size);
                }
                relayed = None;
                // The holder's final picture is its next snapshot: ask now, on this connection.
                // A relay started after the end depends on the index.
                exited = true;
                // The backend drops an ended session's frames, this picture too, until the Mac's
                // heartbeat reports the kept screen, and its acks list no viewer meanwhile. So this
                // relay stays through that, and the report does not wait for a pass to ask the
                // holder: the backend then asks for the picture again (a watch nudge).
                ctl.hold();
                let exit: Value = serde_json::from_slice(&payload).unwrap_or_default();
                if let Some(holder) = ctl.holder.lock().ok().and_then(|holder| holder.clone()) {
                    super::session_states::holder_ended(&holder, exit["code"] == 0);
                }
                if let Some(whole) = exit["above"].as_u64().filter(|_| exit["startOver"] == true) {
                    out.start_over(history, size, whole as usize);
                }
                ctl.snapshot.store(true, SeqCst);
                last_snapshot = Instant::now().checked_sub(SNAPSHOT_GAP).unwrap_or_else(Instant::now);
            }
            Ok(_) | Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) if exited => return Ok(()),
            Err(RecvTimeoutError::Disconnected) => return Err("the holder closed".into()),
        }
        if leaving.is_some_and(|at| at.elapsed() >= ctl.leave_hold) {
            // No exit followed: the screen it left for is the picture.
            leaving = None;
            relayed = None;
            ctl.snapshot.store(true, SeqCst);
        }
        // Only with room: a tick that waits merges into the next one, never queues behind it.
        // And only on a whole picture (`Relayed::ready`): never half a redraw.
        if last_tick.elapsed() >= TICK && leaving.is_none() && out.backlog.is_empty() && !out.stale && out.budget.left() > 0.0
            && relayed.as_mut().is_none_or(|r| r.ready(Instant::now())) {
            last_tick = Instant::now();
            if send_changes(&mut relayed, &mut out, size) {
                last_ping = Instant::now();
            }
        }
    }
}

/// Sends what changed on the session's screen since the viewers' last frame; true when anything had.
fn send_changes(relayed: &mut Option<Relayed>, out: &mut Out, size: (u16, u16)) -> bool {
    let Some(bytes) = relayed.as_mut().map(Relayed::tick).filter(|bytes| !bytes.is_empty()) else { return false };
    out.emit("output", &bytes, size);
    true
}

/// The backend's cap on a snapshot's `parts`.
const MAX_PARTS: usize = 16;

/// A snapshot that does not fit one frame continues as "output" frames with the next seqs, its first
/// frame saying how many there are (`parts`, so a viewer paints it whole; more than MAX_PARTS go
/// unmarked and the viewer paints what came after a wait); history stays "history".
fn frames_for(ids: (&str, &str), epoch: u64, seq: &mut u64, size: (u16, u16), kind: &str, bytes: &[u8]) -> Vec<Value> {
    let chunks: Vec<&[u8]> = if bytes.is_empty() { vec![&[]] } else { bytes.chunks(CHUNK).collect() };
    let parts = chunks.len();
    chunks
        .into_iter()
        .enumerate()
        .map(|(i, chunk)| {
            let mut frame = json!({"type": "local.session.screen", "sessionId": ids.0, "generation": ids.1,
                "epoch": epoch, "seq": *seq, "cols": size.0, "rows": size.1,
                "kind": if i == 0 || kind == "history" { kind } else { "output" },
                "data": base64::engine::general_purpose::STANDARD.encode(chunk)});
            if i == 0 && kind == "snapshot" && (2..=MAX_PARTS).contains(&parts) {
                frame["parts"] = parts.into();
            }
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
        let ids = ("aaaabbbb-cccc-4ddd-8eee-ffff00001111", generation.as_str());
        let mut seq = 7;
        let frames = frames_for(ids, 1_760_000_000_000, &mut seq, (120, 40), "snapshot", &vec![0xffu8; 30 * 1024]);
        assert_eq!(frames.iter().map(|f| f["kind"].as_str().unwrap()).collect::<Vec<_>>(), ["snapshot", "output", "output"]);
        assert_eq!(frames.iter().map(|f| f["seq"].as_u64().unwrap()).collect::<Vec<_>>(), [7, 8, 9]);
        assert_eq!(seq, 10);
        assert_eq!(frames[0]["parts"], 3, "a snapshot in parts says how many");
        let golden: Value = serde_json::from_str(GOLDEN).unwrap();
        let keys = |v: &Value| v.as_object().unwrap().keys().cloned().collect::<BTreeSet<_>>();
        for (i, f) in frames.iter().enumerate() {
            let mut want = keys(&golden);
            if i == 0 {
                want.insert("parts".into());
            }
            assert_eq!(keys(f), want, "`parts` on the snapshot's head only");
            assert!(super::super::ascii_wire_len(f).unwrap() <= 16384);
        }
        // One frame, history, or more parts than the backend takes: the golden shape exactly.
        let one = frames_for(ids, 1, &mut 0, (120, 40), "snapshot", b"x");
        let history = frames_for(ids, 1, &mut 0, (120, 40), "history", &vec![b'h'; 30 * 1024]);
        let many = frames_for(ids, 1, &mut 0, (120, 40), "snapshot", &vec![b's'; (MAX_PARTS + 1) * CHUNK]);
        assert_eq!(many.len(), MAX_PARTS + 1);
        assert!(one.iter().chain(&history).chain(&many).all(|f| keys(f) == keys(&golden)));
    }

    #[test]
    fn screen_relay_sends_the_snapshot_then_the_changes_in_order_and_asks_again_after_a_resize() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"\x1b[Hscreen"))).unwrap();
        tx.send((OUTPUT, with_offset(9, &vec![b'a'; 12 * 1024]))).unwrap();
        tx.send((RESIZE, vec![0, 90, 0, 30])).unwrap();
        tx.send((OUTPUT, with_offset(12_297, b"after"))).unwrap();
        tx.send((EXIT, b"{}".to_vec())).unwrap();
        drop(tx); // an attach holder: it closes with its client, and that is no error
        let sent = std::cell::RefCell::new(vec![]);
        let mut to_holder = vec![];
        pump(&rx, &mut to_holder, ("sid", "gen"), (120, 40), &Ctl::new(0), &|f| { sent.borrow_mut().push(f); true }, Budget::new(), &|_| None).unwrap();
        let sent = sent.into_inner();
        assert_eq!(sent[0]["kind"], "snapshot");
        assert!(sent.len() > 1 && sent[1..].iter().all(|f| f["kind"] == "output" && f["cols"] == 120),
            "the changes go at the size they were made at; the new size waits for its snapshot");
        assert_eq!(sent.iter().map(|f| f["seq"].as_u64().unwrap()).collect::<Vec<_>>(), (0..sent.len() as u64).collect::<Vec<_>>());
        let mut raw = vt100::Parser::new(40, 120, 0);
        raw.process(b"\x1b[Hscreen");
        raw.process(&vec![b'a'; 12 * 1024]);
        assert_eq!(viewer(&sent, (120, 40)).screen().contents(), raw.screen().contents());
        assert!(to_holder.windows(5).any(|w| w == encode(SNAPSHOT, b"").as_slice()), "a resize asks for a fresh screen");
    }

    #[test]
    fn screen_relay_stays_on_a_holder_whose_session_ended_and_sends_its_final_picture() {
        // Claude Code 2.1.292 on `/exit` (captured 2026-10-07): it leaves the alternate screen and
        // prints the resume lines, then exits. The holder's final picture (`Model::ended_picture`)
        // only ever comes in a snapshot asked for after the exit.
        use crate::holder::model::CLAUDE_EXIT_READS;
        let has = |hay: &[u8], needle: &[u8]| hay.windows(needle.len()).any(|w| w == needle);
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"\x1b[?1049hthe conversation"))).unwrap();
        for read in CLAUDE_EXIT_READS {
            tx.send((OUTPUT, with_offset(24, read))).unwrap();
        }
        tx.send((EXIT, b"{}".to_vec())).unwrap();
        let ctl = leased(Duration::from_millis(1500));
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(60, b"the conversation"))], asked: vec![] };
        let sent = std::cell::RefCell::new(vec![]);
        let started = Instant::now();
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push(f); true }, Budget::new(), &|_| None).unwrap();
        let sent = sent.into_inner();
        let data = |f: &Value| base64::engine::general_purpose::STANDARD.decode(f["data"].as_str().unwrap()).unwrap();
        // Prod 2026-10-07: a viewer read "Resume this session with: claude --resume ..." as the
        // session's picture for 7 s. What the provider prints after it leaves the alternate screen
        // on its way out never goes to a viewer: the picture before it stays, then the kept one.
        // (The reads before the leave change only modes; a tick that fell between them would send those.)
        let snapshots: Vec<&Value> = sent.iter().filter(|f| f["kind"] == "snapshot").collect();
        assert_eq!((snapshots.len(), sent[0]["kind"].as_str()), (2, Some("snapshot")));
        assert!(sent.iter().all(|f| !has(&data(f), b"Resume this session") && !has(&data(f), b"\x1b[?1049l")));
        assert_eq!(data(sent.last().unwrap()), b"the conversation", "the viewer is left on the final picture");
        assert_eq!(holder.asked.len(), 1);
        assert!(holder.asked[0] - started < Duration::from_millis(500), "asked at the exit, not a beat later");
        assert!(started.elapsed() >= Duration::from_millis(1400), "the relay left at the exit; a new one may find no screen indexed yet");
        // Prod 2026-10-07: an ack built before the kept screen was reported lists no viewer of the
        // ended session. It stopped the relay, and the next one's new run cost the viewer its scrollback.
        assert!(ctl.kept(), "acks that do not list the ended session yet do not stop its relay");
    }

    #[test]
    fn screen_relay_sends_a_snapshot_after_a_leave_of_the_alternate_screen_when_no_exit_follows() {
        // A full-screen program that closed mid-session (an editor, a pager): the session runs on,
        // and its normal screen is the picture. Late by the hold, never lost.
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"\x1b[?1049heditor"))).unwrap();
        tx.send((OUTPUT, with_offset(14, b"saved\x1b[?10"))).unwrap();
        tx.send((OUTPUT, with_offset(26, b"49l$ back at the prompt"))).unwrap();
        let ctl = Ctl { leave_hold: Duration::from_millis(600), ..leased(Duration::from_millis(1500)) };
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(49, b"$ back at the prompt"))], asked: vec![] };
        let sent = std::cell::RefCell::new(vec![]);
        let started = Instant::now();
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push((started.elapsed(), f)); true }, Budget::new(), &|_| None).unwrap();
        let sent = sent.into_inner();
        assert_eq!(sent.iter().map(|(_, f)| f["kind"].as_str().unwrap()).collect::<Vec<_>>(), ["snapshot", "snapshot"], "a leave split across two reads is still found");
        assert_eq!(data(&sent[1].1), b"$ back at the prompt");
        assert_eq!(holder.asked.len(), 1);
        assert!(holder.asked[0] - started >= Duration::from_millis(550) && sent[1].0 < Duration::from_millis(1100), "held for the hold, then asked: {:?}", sent[1].0);
    }

    /// Prod 2026-10-08, a held Codex session, 101 `/status` blocks: twice a viewer lost one block's
    /// 19 rows, each time where a snapshot reached the relay before the block's output had gone.
    /// Codex writes finished lines above its prompt through a scroll region, so a terminal keeps
    /// them only when they are pushed into it: the snapshot is the screen after them.
    #[test]
    fn rows_pushed_off_the_top_before_a_snapshot_go_before_it() {
        // Codex's `insert_history`: region above the prompt, to its last row, lines, region off, prompt.
        let block = b"\x1b[1;25r\x1b[25;1H\r\n/status\r\n  Session: 01a1\r\n  Context window: 97% left\x1b[r\x1b[27;1H> ";
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"screen"))).unwrap();
        tx.send((OUTPUT, with_offset(6, block))).unwrap();
        // A snapshot in the same tick as the output: nothing was ticked yet.
        tx.send((SNAPSHOT, with_offset(6 + block.len() as u64, b"screen after the block"))).unwrap();
        tx.send((OUTPUT, with_offset(6 + block.len() as u64, b"next"))).unwrap();
        tx.send((EXIT, b"{}".to_vec())).unwrap();
        drop(tx);
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut Vec::<u8>::new(), ("sid", "gen"), (100, 30), &Ctl::new(0), &|f| { sent.borrow_mut().push(f); true }, Budget::new(), &|_| None).unwrap();
        let sent = sent.into_inner();
        assert_eq!(kinds(&sent), [("snapshot".into(), 0), ("output".into(), 1), ("snapshot".into(), 2), ("output".into(), 3)]);
        assert_eq!(data(&sent[2]), b"screen after the block");
        // The viewer before that snapshot: the screen the bytes make, and the rows they scrolled
        // off the top of the region in its scrollback, oldest first (a terminal fed the bytes
        // keeps them there too; vt100 does not for a region, so it is no reference for that).
        let mut v = viewer(&sent[..2], (100, 30));
        let mut raw = vt100::Parser::new(30, 100, 0);
        raw.process(b"screen");
        raw.process(block);
        assert_eq!(v.screen().contents(), raw.screen().contents());
        v.screen_mut().set_scrollback(usize::MAX);
        assert_eq!(v.screen().scrollback(), 3, "the three rows the block's line feeds scrolled off");
        assert_eq!(v.screen().rows(0, 100).next().unwrap(), "screen");
        assert!(has(&data(&sent[3]), b"next"));
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
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|_| false, Budget::new(), &|_| None).unwrap();
        assert!(started.elapsed() < Duration::from_secs(4));
        assert!((2..=3).contains(&holder.asked.len()), "asked {} times, not every tick", holder.asked.len());
        for pair in holder.asked.windows(2) {
            assert!(pair[1] - pair[0] >= SNAPSHOT_GAP - Duration::from_millis(10));
        }
    }

    #[test]
    fn output_over_the_budget_waits_for_it_and_no_frame_is_skipped() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, &vec![b's'; 4096]))).unwrap();
        tx.send((OUTPUT, with_offset(4096, &vec![b'a'; 100]))).unwrap();
        tx.send((OUTPUT, with_offset(4196, b"more"))).unwrap();
        let ctl = leased(Duration::from_millis(3000));
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(4200, b"fresh"))], asked: vec![] };
        // Empty, refilling 2000 bytes a second: the snapshot overdraws it to -4096, so the change
        // goes after about 2.1 s.
        let budget = Budget { tokens: 0.0, cap: 64.0 * 1024.0, per_s: 2000.0, at: Instant::now() };
        let sent = std::cell::RefCell::new(vec![]);
        let started = Instant::now();
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push((started.elapsed(), f)); true }, budget, &|_| None).unwrap();
        let sent = sent.into_inner();
        let frames: Vec<Value> = sent.iter().map(|(_, f)| f.clone()).collect();
        // Before: seq 1 was skipped, "more" never went, and a snapshot repaired the gap, so the
        // lines that output scrolled off the top were in no viewer's scrollback.
        assert_eq!(kinds(&frames), [("snapshot".into(), 0), ("output".into(), 1)]);
        let mut raw = vt100::Parser::new(24, 80, 0);
        raw.process(&vec![b's'; 4096]);
        raw.process(&vec![b'a'; 100]);
        raw.process(b"more");
        assert_eq!(viewer(&frames, (80, 24)).screen().contents(), raw.screen().contents(), "every change, in one frame");
        assert!(sent[1].0 >= Duration::from_millis(1800), "sent once the budget was back: {:?}", sent[1].0);
        assert!(holder.asked.is_empty(), "nothing was lost, so nothing is repaired");
    }

    /// Prod 2026-10-08 08:50:07Z: "connection loop iteration took 6.1s; slowest step: uploads". The
    /// relay queue (8 frames) was full meanwhile; the beat's snapshot and 4 Codex blocks were lost
    /// there, two snapshots repaired the viewer's screen, and its scrollback never got the 82 rows.
    #[test]
    fn output_the_relay_queue_has_no_room_for_waits_and_goes_out_in_order_without_a_gap() {
        let ctl = Ctl::new(0);
        let room = std::cell::Cell::new(false);
        let sent = std::cell::RefCell::new(vec![]);
        let send = |f: Value| room.get() && { sent.borrow_mut().push(f); true };
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &send, budget: Budget::new(), backlog: Default::default(), stale: false };
        let data = |f: &Value| base64::engine::general_purpose::STANDARD.decode(f["data"].as_str().unwrap()).unwrap();
        // The connection loop is stalled: nothing finds room.
        out.emit("output", b"block 1, ", (100, 30));
        out.emit("snapshot", b"the screen after block 1", (100, 30));
        assert!(ctl.snapshot.swap(false, SeqCst), "a snapshot that cannot go is asked for again");
        out.emit("output", b"block 2, ", (100, 30));
        out.emit("output", b"block 3", (100, 30));
        assert!(sent.borrow().is_empty() && out.seq == 0 && !out.stale, "nothing went, no seq is taken, nobody waits for a repair");
        // It runs again: the pump's next tick.
        room.set(true);
        out.flush();
        out.emit("snapshot", b"the screen after block 3", (100, 30));
        assert_eq!(kinds(&sent.borrow()), [("output".into(), 0), ("snapshot".into(), 1)]);
        assert_eq!(data(&sent.borrow()[0]), b"block 1, block 2, block 3", "all of it, in order, once");
        assert_eq!(data(&sent.borrow()[1]), b"the screen after block 3");
        // Output written at another size keeps its own.
        room.set(false);
        out.emit("output", b"wide", (100, 30));
        out.emit("output", b"narrow", (80, 24));
        room.set(true);
        out.flush();
        let shape: Vec<(Vec<u8>, u64)> = sent.borrow()[2..].iter().map(|f| (data(f), f["cols"].as_u64().unwrap())).collect();
        assert_eq!(shape, [(b"wide".to_vec(), 100), (b"narrow".to_vec(), 80)]);
    }

    #[test]
    fn a_snapshot_cut_short_by_a_full_queue_is_finished_as_the_output_it_is() {
        let ctl = Ctl::new(0);
        let calls = std::cell::Cell::new(0);
        let sent = std::cell::RefCell::new(vec![]);
        // Room for the first frame, none for the second, then room again.
        let send = |f: Value| { calls.set(calls.get() + 1); calls.get() != 2 && { sent.borrow_mut().push(f); true } };
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &send, budget: Budget::new(), backlog: Default::default(), stale: false };
        let screen: Vec<u8> = (0..2 * CHUNK + 5).map(|i| (i % 251) as u8).collect();
        out.emit("snapshot", &screen, (120, 40));
        out.emit("output", b"after", (120, 40));
        assert_eq!(kinds(&sent.borrow())[..3], [("snapshot".into(), 0), ("output".into(), 1), ("output".into(), 2)]);
        let all: Vec<u8> = sent.borrow().iter().flat_map(|f| base64::engine::general_purpose::STANDARD.decode(f["data"].as_str().unwrap()).unwrap()).collect();
        assert_eq!(all, [screen, b"after".to_vec()].concat(), "the whole screen, then the output after it");
        assert!(!out.stale && !ctl.snapshot.load(SeqCst));
    }

    #[test]
    fn more_output_than_may_wait_is_given_up_with_a_gap_and_a_snapshot_follows() {
        let ctl = Ctl::new(0);
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 4, ctl: &ctl, send: &|_| false, budget: Budget::new(), backlog: Default::default(), stale: false };
        out.emit("output", &vec![b'x'; BACKLOG], (80, 24));
        assert!(!out.stale && out.seq == 4 && !ctl.snapshot.load(SeqCst), "this much still waits");
        out.emit("output", b"y", (80, 24));
        assert!(out.stale && out.backlog.is_empty() && ctl.snapshot.load(SeqCst), "past it the viewers get the screen as it is");
        assert_eq!(out.seq, 5, "the skipped seq tells viewers a frame is lost");
        out.emit("output", b"z", (80, 24));
        assert!(out.backlog.is_empty(), "output until that snapshot is in it");
    }

    /// Review of #3912: with output still waiting for the queue, a new viewer's history went out
    /// at once, with the next seqs. It is the holder's lines AFTER that output, so the output, sent
    /// later, drew over them again in every viewer.
    #[test]
    fn a_history_burst_goes_out_behind_the_output_that_waits_and_never_ahead_of_it() {
        let ctl = Ctl::new(0);
        let calls = std::cell::Cell::new(0);
        let sent = std::cell::RefCell::new(vec![]);
        // The queue is full for the first three tries.
        let send = |f: Value| { calls.set(calls.get() + 1); calls.get() > 3 && { sent.borrow_mut().push(f); true } };
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &send, budget: Budget::new(), backlog: Default::default(), stale: false };
        out.emit("output", b"waiting", (80, 24));
        assert!(sent.borrow().is_empty() && !out.backlog.is_empty());
        // A viewer arrives.
        assert!(out.history(&|_| Some(b"lines".to_vec()), (80, 24)));
        assert_eq!(kinds(&sent.borrow()), [("output".into(), 0), ("history".into(), 1)]);
        assert!(!out.stale && out.backlog.is_empty());
    }

    #[test]
    fn output_given_up_while_a_viewer_waits_is_repaired_first_and_the_history_follows_the_snapshot() {
        let ctl = Ctl::new(0);
        let room = std::cell::Cell::new(false);
        let sent = std::cell::RefCell::new(vec![]);
        let send = |f: Value| room.get() && { sent.borrow_mut().push(f); true };
        let asked = std::cell::Cell::new(0);
        let fetch = |_: usize| { asked.set(asked.get() + 1); Some(b"lines".to_vec()) };
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &send, budget: Budget::new(), backlog: Default::default(), stale: false };
        out.emit("output", b"waiting", (80, 24));
        // The queue stays full: the viewer is not held longer than PACE.
        let started = Instant::now();
        assert!(!out.history(&fetch, (80, 24)), "to be asked for again");
        assert!(started.elapsed() >= PACE && started.elapsed() < PACE + Duration::from_millis(500));
        assert!(out.stale && out.backlog.is_empty() && out.seq == 1 && ctl.snapshot.swap(false, SeqCst), "the output is given up: a gap, and a snapshot is asked for");
        assert_eq!(asked.get(), 0, "no lines were read for a burst that did not go");
        // Still no history while that snapshot is owed, queue or no queue.
        room.set(true);
        assert!(!out.history(&fetch, (80, 24)) && sent.borrow().is_empty());
        out.emit("snapshot", b"screen", (80, 24));
        assert!(out.history(&fetch, (80, 24)));
        assert_eq!(kinds(&sent.borrow()), [("snapshot".into(), 1), ("history".into(), 2)]);
        assert_eq!(asked.get(), 1);
    }

    #[test]
    fn a_session_that_ends_with_output_still_waiting_never_shows_its_kept_picture_ahead_of_it() {
        let data = |f: &Value| base64::engine::general_purpose::STANDARD.decode(f["data"].as_str().unwrap()).unwrap();
        let run = |above: Option<Vec<u8>>| {
            let ctl = Ctl::new(0);
            let room = std::cell::Cell::new(false);
            let sent = std::cell::RefCell::new(vec![]);
            // No room for output yet (the budget is short, or the queue just filled).
            let send = |f: Value| (room.get() || f["kind"] != "output") && { sent.borrow_mut().push(f); true };
            let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 7, ctl: &ctl, send: &send, budget: Budget::new(), backlog: Default::default(), stale: false };
            out.emit("output", b"last lines", (80, 24));
            // EXIT with `startOver`, as `pump` takes it; then the holder's kept picture.
            out.start_over(&|_| above.clone(), (80, 24), 5);
            out.emit("snapshot", b"kept", (80, 24));
            let asked_again = ctl.snapshot.swap(false, SeqCst);
            room.set(true);
            out.flush();
            if asked_again {
                out.emit("snapshot", b"kept", (80, 24));
            }
            let frames: Vec<(String, u64, Vec<u8>, bool)> = sent.borrow().iter()
                .map(|f| (f["kind"].as_str().unwrap().to_owned(), f["seq"].as_u64().unwrap(), data(f), f["epoch"].as_u64().unwrap() == 1)).collect();
            frames
        };
        // The run goes on (not all the lines above the picture came): the picture waits behind the output.
        assert_eq!(run(None), [("output".to_owned(), 7, b"last lines".to_vec(), true), ("snapshot".to_owned(), 8, b"kept".to_vec(), true)]);
        // The viewers start over: the old run's waiting output is dropped, never sent into the new run.
        assert_eq!(run(Some(b"above".to_vec())), [("history".to_owned(), 0, b"above".to_vec(), false), ("snapshot".to_owned(), 1, b"kept".to_vec(), false)]);
    }

    /// A holder that keeps its lines: it answers HISTORY with `lines[n]` for the n-th request (output
    /// it wrote just before, then its lines, then the screen behind them) and every snapshot request
    /// with its screen.
    struct Keeping {
        tx: std::sync::mpsc::Sender<(u8, Vec<u8>)>,
        lines: Vec<Vec<(u8, Vec<u8>)>>,
        asked: Vec<(u8, u64)>,
    }

    impl Write for Keeping {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            let (kind, payload) = read_frame(&mut &buf[..]).unwrap();
            let answer = if kind == HISTORY {
                self.lines.get(self.asked.iter().filter(|(kind, _)| *kind == HISTORY).count()).cloned().unwrap_or_default()
            } else {
                vec![(SNAPSHOT, with_offset(0, b"asked screen"))]
            };
            self.asked.push((kind, split_offset(&payload).map_or(0, |(max, _)| max)));
            for frame in answer {
                let _ = self.tx.send(frame);
            }
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    /// Review of #3912: the lines were read on another connection while output waited in this
    /// relay's 200 ms buffer, or unread in its channel. The holder's model had that output already,
    /// so it went out again after the history, and a viewer who joined then drew it twice.
    #[test]
    fn output_that_reaches_the_relay_before_the_holders_lines_is_sent_before_them_and_never_twice() {
        let (tx, rx) = std::sync::mpsc::channel();
        // The holder's unasked first screen, then output in the same tick as the new viewer.
        tx.send((SNAPSHOT, with_offset(0, b"first screen"))).unwrap();
        tx.send((OUTPUT, with_offset(0, b"block one"))).unwrap();
        let ctl = Arc::new(leased(Duration::from_millis(1500)));
        ctl.lines.store(true, SeqCst);
        // A second viewer arrives later; the holder writes more just before it answers.
        let later = ctl.clone();
        std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(500)); later.history.store(true, SeqCst); });
        let screen = |text: &[u8]| (SNAPSHOT, with_offset(0, text));
        let mut holder = Keeping { tx, asked: vec![], lines: vec![
            vec![(HISTORY, b"lines with block one\r\n".to_vec()), screen(b"screen one")],
            vec![(OUTPUT, with_offset(9, b"block two")), (HISTORY, b"lines with block two\r\n".to_vec()), screen(b"screen two")],
        ] };
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push(f); true }, Budget::new(),
            &|_| panic!("a holder that keeps its lines is never asked on another connection")).unwrap();
        let sent = sent.into_inner();
        let text = |f: &Value| String::from_utf8(data(f)).unwrap();
        let drawn: Vec<(String, String)> = sent.iter().filter(|f| f["kind"] != "ping").map(|f| (f["kind"].as_str().unwrap().to_owned(), text(f))).collect();
        let frame = |kind: &str, text: &str| (kind.to_owned(), text.to_owned());
        // Block one reached the relay before any screen did: no viewer has a screen to change, and
        // the lines and the screen behind them show it. Block two changes the screen they have.
        assert!(drawn[2].0 == "output" && drawn[2].1.contains("block two"), "{drawn:?}");
        assert_eq!([&drawn[..2], &drawn[3..]].concat(), [
            // Not "first screen": it is older than the lines asked for, and the one behind them is the picture.
            frame("history", "lines with block one\r\n"), frame("snapshot", "screen one"),
            frame("history", "lines with block two\r\n"), frame("snapshot", "screen two"),
        ]);
        assert_eq!(sent.iter().map(|f| f["seq"].as_u64().unwrap()).collect::<Vec<_>>(), (0..5).collect::<Vec<_>>(), "one run, no gap");
        assert_eq!(holder.asked.iter().map(|(kind, _)| *kind).collect::<Vec<_>>(), [HISTORY, HISTORY], "the screen behind the lines is the picture: none is asked for");
        assert_eq!(holder.asked[0].1, (BURST - HISTORY_RESERVE) as u64, "asked for what the budget has room for");
    }

    #[test]
    fn a_holder_that_never_sends_its_lines_still_gives_the_viewers_a_screen() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"first screen"))).unwrap();
        let ctl = leased(LINES_WAIT + Duration::from_millis(800));
        ctl.lines.store(true, SeqCst);
        let mut holder = Keeping { tx, asked: vec![], lines: vec![] };
        let sent = std::cell::RefCell::new(vec![]);
        let started = Instant::now();
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push((started.elapsed(), f)); true }, Budget::new(), &|_| None).unwrap();
        let sent = sent.into_inner();
        assert_eq!(sent.iter().map(|(_, f)| f["kind"].as_str().unwrap()).collect::<Vec<_>>(), ["snapshot"]);
        assert!(sent[0].0 >= LINES_WAIT && sent[0].0 < LINES_WAIT + Duration::from_millis(500), "{:?}", sent[0].0);
    }

    /// Review of #3912: output sent while the holder's lines were on their way left the budget
    /// less room than was asked for, and the answer was cut by bytes: inside a character or a
    /// colour sequence.
    #[test]
    fn lines_that_no_longer_all_fit_lose_whole_lines_from_the_old_end_never_part_of_one() {
        // As `Model::history` writes them: each row from the default attributes, a reset, CR LF;
        // then the line feeds that push the last line off an empty screen.
        let rows = ["\x1b[38;2;99;168;248mérable \u{4e2d}\u{6587} one", "plain two", "\x1b[1m\u{1f600} three\x1b[22m \x1b[48;2;49;49;53m!", "four"];
        let each: Vec<Vec<u8>> = rows.iter().map(|row| format!("{row}\x1b[m\r\n").into_bytes()).collect();
        let feeds = b"\n\n\n";
        let all: Vec<u8> = each.iter().flatten().copied().chain(feeds.iter().copied()).collect();
        for room in 0..=all.len() + 1 {
            let kept = newest_lines(&all, room);
            assert!(kept.len() <= room.min(all.len()), "{room}");
            // Always some of the newest lines, whole, then the feeds; the most that fit.
            let lines = (0..=each.len()).find(|skip| each[*skip..].iter().map(Vec::len).sum::<usize>() + feeds.len() <= room);
            let expected: Vec<u8> = match lines {
                Some(skip) => each[skip..].iter().flatten().copied().chain(feeds.iter().copied()).collect(),
                None => vec![],
            };
            assert_eq!(kept, expected, "room {room}");
        }
        assert_eq!(newest_lines(b"", 10), b"");
        // In the relay: 20 bytes of room when the answer comes, for lines asked for with more.
        let ctl = Ctl::new(0);
        let sent = std::cell::RefCell::new(vec![]);
        let send = |f: Value| { sent.borrow_mut().push(f); true };
        let budget = Budget { tokens: (HISTORY_RESERVE + 20) as f64, cap: BURST as f64, per_s: 0.0, at: Instant::now() };
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &send, budget, backlog: Default::default(), stale: false };
        assert!(out.lines(&all, (80, 24)));
        let data = base64::engine::general_purpose::STANDARD.decode(sent.borrow()[0]["data"].as_str().unwrap()).unwrap();
        assert_eq!(data, b"four\x1b[m\r\n\n\n\n", "the last line whole, not the last 20 bytes");
        // No room for even one line: nothing is sent, which is not "there are no lines".
        assert!(out.lines(&all, (80, 24)) && sent.borrow().len() == 1);
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
        nudge_relay(&mut relays, sid.clone(), None, &fake);
        assert_eq!(*spawned.borrow(), [(sid.clone(), u64::MAX)], "started at once, before any ack");
        settle(&mut relays, &BTreeMap::new(), &fake);
        assert!(relays.contains_key(&sid), "an ack built before the viewer arrived does not stop it");
        // The first ack that lists it renews it without a second snapshot.
        settle(&mut relays, &BTreeMap::from([(sid.clone(), 1_760_000_000)]), &fake);
        assert!(!relays[&sid].snapshot.load(SeqCst));
        assert_eq!(relays[&sid].since.load(SeqCst), 1_760_000_000);
        assert!(!relays[&sid].history.load(SeqCst));
        settle(&mut relays, &BTreeMap::from([(sid.clone(), 1_760_000_001)]), &fake);
        assert!(relays[&sid].history.load(SeqCst), "a new viewer arrived: history, then a snapshot");
        *relays[&sid].keep.lock().unwrap() = Instant::now();
        let ctl = relays[&sid].clone();
        settle(&mut relays, &BTreeMap::new(), &fake);
        assert!(!relays.contains_key(&sid) && ctl.stop.load(SeqCst), "after the grace the ack rules again");
        assert_eq!(spawned.borrow().len(), 1);
        // A nudge for a running relay asks it for a fresh screen instead of starting another.
        let mut relays = BTreeMap::from([(sid.clone(), Arc::new(Ctl::new(5)))]);
        nudge_relay(&mut relays, sid.clone(), None, &fake);
        assert!(relays[&sid].history.load(SeqCst) && relays[&sid].kept());
        assert_eq!(spawned.borrow().len(), 1);
    }

    #[test]
    fn a_nudge_carries_the_viewers_since_so_the_ack_that_lists_them_asks_for_no_second_history() {
        let sid = "aaaabbbb-cccc-4ddd-8eee-ffff00001111".to_owned();
        let fake = |_: String, since: u64| Arc::new(Ctl::new(since));
        let ack = |since: u64| BTreeMap::from([(sid.clone(), since)]);
        // A relay with one viewer since 50; a second arrives at 100 and the backend nudges.
        let mut relays = BTreeMap::from([(sid.clone(), Arc::new(Ctl::new(50)))]);
        nudge_relay(&mut relays, sid.clone(), Some(100), &fake);
        assert_eq!(relays[&sid].since.load(SeqCst), 100);
        assert!(relays[&sid].history.swap(false, SeqCst), "one history request, which the pump takes");
        // An ack built before the viewer arrived lands after the nudge: `since` is not lowered.
        settle(&mut relays, &ack(50), &fake);
        assert_eq!(relays[&sid].since.load(SeqCst), 100);
        settle(&mut relays, &ack(100), &fake);
        assert!(!relays[&sid].history.load(SeqCst), "the ack that lists the nudged viewer is not news");
        settle(&mut relays, &ack(101), &fake);
        assert!(relays[&sid].history.load(SeqCst), "a later viewer still is");
        // A nudge that starts the relay: its first ack is not news either.
        let mut relays = BTreeMap::new();
        nudge_relay(&mut relays, sid.clone(), Some(100), &fake);
        settle(&mut relays, &ack(100), &fake);
        assert!(!relays[&sid].history.load(SeqCst) && relays[&sid].since.load(SeqCst) == 100);
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
                watch(&mut mac, &sid, "gen-1", &theirs, &|f| { out.lock().unwrap().push(f); true }, &|_| None).unwrap();
            });
            ctl
        };
        let mut relays = BTreeMap::new();
        nudge_relay(&mut relays, sid.clone(), None, attach); // no `offer`, no ack
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
    fn a_codex_attach_terminal_lives_while_its_thread_is_live() {
        const THREAD: &str = "01a10845-c8f1-7fa2-844b-13790e2db151";
        let claude = BTreeSet::from(["11111111".to_owned()]);
        let holders = [attach("aaaaaaaa", THREAD, 10), attach("bbbbbbbb", "11111111", 20)];
        let running = running_jobs(Some(&claude), codex_running(true, [THREAD].into_iter(), &holders)).unwrap();
        assert_eq!(running, BTreeSet::from(["11111111".to_owned(), THREAD.to_owned()]));
        assert!(running_jobs(None, codex_running(true, [THREAD].into_iter(), &holders)).is_none(), "a failed Claude listing is no evidence for anything");
        let (mut wanted, none, t0) = (BTreeMap::new(), BTreeSet::new(), Instant::now());
        assert!(idle_attach(&holders, Some("aaaaaaaa"), &none, Some(&running), &mut wanted, t0).is_empty());
        // The thread's mirror ended (Codex's daemon let its rollout go): the client is closed even while shown.
        let ended = running_jobs(Some(&claude), codex_running(true, std::iter::empty(), &holders)).unwrap();
        assert_eq!(pids(idle_attach(&holders, Some("aaaaaaaa"), &none, Some(&ended), &mut wanted, t0)), [10]);
        // An app relaunch: no mirror is published yet, so an open Codex holder is not judged ended.
        let relaunch = running_jobs(Some(&claude), codex_running(false, std::iter::empty(), &holders)).unwrap();
        assert!(idle_attach(&holders, None, &none, Some(&relaunch), &mut BTreeMap::new(), t0).is_empty());
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

    fn data(f: &Value) -> Vec<u8> {
        base64::engine::general_purpose::STANDARD.decode(f["data"].as_str().unwrap()).unwrap()
    }

    fn has(hay: &[u8], needle: &[u8]) -> bool {
        hay.windows(needle.len()).any(|w| w == needle)
    }

    fn kinds(sent: &[Value]) -> Vec<(String, u64)> {
        sent.iter().map(|f| (f["kind"].as_str().unwrap().into(), f["seq"].as_u64().unwrap())).collect()
    }

    #[test]
    fn a_new_run_sends_history_before_the_holders_first_snapshot_and_leaves_room_for_it() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(30_720, b"\x1b[Hscreen"))).unwrap();
        tx.send((EXIT, b"{}".to_vec())).unwrap();
        drop(tx);
        let asked = std::cell::Cell::new(0);
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut Vec::<u8>::new(), ("sid", "gen"), (120, 40), &Ctl::new(0), &|f| { sent.borrow_mut().push(f); true }, Budget::new(),
            &|max| { asked.set(max); Some(vec![b'h'; 30 * 1024]) }).unwrap();
        assert_eq!(asked.get(), BURST - HISTORY_RESERVE);
        assert_eq!(kinds(&sent.borrow()), [("history".into(), 0), ("history".into(), 1), ("history".into(), 2), ("snapshot".into(), 3)]);
    }

    /// Prod 2026-10-08: after `/exit` a viewer's 30-row terminal showed the kept picture in its top 12
    /// rows and 18 blank rows under it. The picture is now the whole screen at rest, which shows again
    /// lines the way out pushed into that viewer's scrollback: the holder says so, and the viewer is
    /// drawn from the start, the lines above that picture first.
    #[test]
    fn a_session_that_ends_on_a_picture_of_lines_its_viewers_have_starts_them_over() {
        let run = |whole: usize, above: Option<Vec<u8>>| {
            let (tx, rx) = std::sync::mpsc::channel();
            tx.send((SNAPSHOT, with_offset(0, b"live"))).unwrap();
            tx.send((EXIT, format!(r#"{{"code":0,"startOver":true,"above":{whole}}}"#).into_bytes())).unwrap();
            let ctl = leased(Duration::from_millis(300));
            let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(4, b"kept"))], asked: vec![] };
            let sent = std::cell::RefCell::new(vec![]);
            let asked = std::cell::RefCell::new(vec![]);
            // The run's own first history (nothing above the screen yet), then the one asked for at the end.
            pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push(f); true }, Budget::new(),
                &|max| { asked.borrow_mut().push(max); if asked.borrow().len() == 1 { Some(vec![]) } else { above.clone() } }).unwrap();
            let sent = sent.into_inner();
            let epochs: Vec<u64> = sent.iter().map(|f| f["epoch"].as_u64().unwrap()).collect();
            (kinds(&sent), epochs, ctl.epoch.load(SeqCst), asked.into_inner())
        };
        let (sent, epochs, typed_against, asked) = run(5, Some(b"above".to_vec()));
        assert_eq!(sent, [("history".into(), 0), ("snapshot".into(), 1), ("history".into(), 0), ("snapshot".into(), 1)]);
        assert!(epochs[0] == epochs[1] && epochs[2] > epochs[1] && epochs[3] == epochs[2], "{epochs:?}");
        assert_eq!(typed_against, epochs[3]);
        assert_eq!(asked[1], 5, "asked for all of it");
        // Nothing is above the picture: that is whole too.
        assert_eq!(run(0, Some(vec![])).0, [("history".into(), 0), ("snapshot".into(), 1), ("history".into(), 0), ("snapshot".into(), 1)]);
        // Review of #3878: a viewer who watched for long has more lines than a trimmed burst gives
        // back. Only part of them came, the holder did not answer, or they are more than the budget
        // has room for (never asked for): the run goes on, and the viewer keeps the scrollback it has.
        for (whole, above) in [(5, Some(b"ove".to_vec())), (5, None), (BURST, Some(vec![b'h'; BURST]))] {
            let (sent, epochs, _, asked) = run(whole, above);
            assert_eq!(sent, [("history".into(), 0), ("snapshot".into(), 1), ("snapshot".into(), 2)], "{whole}");
            assert!(epochs.iter().all(|epoch| *epoch == epochs[0]));
            assert_eq!(asked.len(), if whole == BURST { 1 } else { 2 });
        }
    }

    #[test]
    fn a_grown_viewer_set_gets_history_then_a_fresh_snapshot_and_a_short_budget_trims_history_from_its_old_end() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(10, b"screen"))).unwrap();
        let ctl = Arc::new(leased(Duration::from_millis(1500)));
        let later = ctl.clone();
        std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(200)); later.history.store(true, SeqCst); });
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(10, b"fresh"))], asked: vec![] };
        // No refill: 20 bytes of room at the start, 4 left when the viewer arrives.
        let budget = Budget { tokens: (HISTORY_RESERVE + 20) as f64, cap: BURST as f64, per_s: 0.0, at: Instant::now() };
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push(f); true }, budget,
            &|_| Some(b"0123456789".to_vec())).unwrap();
        let sent = sent.into_inner();
        assert_eq!(kinds(&sent), [("history".into(), 0), ("snapshot".into(), 1), ("history".into(), 2), ("snapshot".into(), 3)]);
        let data = |f: &Value| base64::engine::general_purpose::STANDARD.decode(f["data"].as_str().unwrap()).unwrap();
        assert_eq!((data(&sent[0]), data(&sent[2])), (b"0123456789".to_vec(), b"6789".to_vec()));
        assert_eq!(holder.asked.len(), 1, "one fresh snapshot after the history");
    }

    #[test]
    fn a_quiet_relay_only_pings_and_output_never_asks_for_a_snapshot() {
        let run = |output: bool| {
            let (tx, rx) = std::sync::mpsc::channel();
            tx.send((SNAPSHOT, with_offset(0, b"screen"))).unwrap();
            if output {
                tx.send((OUTPUT, with_offset(6, b"x"))).unwrap();
            }
            let mut ctl = leased(Duration::from_millis(1300));
            ctl.beat = Duration::from_millis(300);
            let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(7, b"screen"))], asked: vec![] };
            let sent = std::cell::RefCell::new(vec![]);
            pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push(f); true }, Budget::new(), &|_| None).unwrap();
            (kinds(&sent.into_inner()), holder.asked.len())
        };
        let (quiet, asked) = run(false);
        assert_eq!(asked, 0, "a quiet screen is never redrawn");
        assert_eq!(quiet[0], ("snapshot".into(), 0));
        assert!(quiet.len() >= 4 && quiet[1..].iter().all(|k| *k == ("ping".into(), 1)), "{quiet:?}");
        let (busy, asked) = run(true);
        assert_eq!(asked, 0, "output is the change itself: nothing to redraw");
        assert_eq!(busy[..2], [("snapshot".into(), 0), ("output".into(), 1)]);
        assert!(busy.len() >= 4 && busy[2..].iter().all(|k| *k == ("ping".into(), 2)), "{busy:?}");
    }

    #[test]
    fn a_history_burst_waits_for_room_in_the_relay_queue() {
        let ctl = Ctl::new(0);
        let calls = std::cell::Cell::new(0);
        let sent = std::cell::RefCell::new(vec![]);
        let send = |f: Value| { calls.set(calls.get() + 1); calls.get() % 2 == 0 && { sent.borrow_mut().push(f); true } };
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &send, budget: Budget::new(), backlog: Default::default(), stale: false };
        out.history(&|_| Some(vec![b'h'; 3 * CHUNK]), (80, 24));
        assert_eq!(kinds(&sent.borrow()), [("history".into(), 0), ("history".into(), 1), ("history".into(), 2)]);
        assert!(!out.stale, "a full queue for a moment is not a lost frame");
    }

    #[test]
    fn nothing_above_the_screen_is_one_empty_history_frame_and_a_silent_holder_is_none() {
        let ctl = Ctl::new(0);
        let sent = std::cell::RefCell::new(vec![]);
        let send = |f: Value| { sent.borrow_mut().push(f); true };
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &send, budget: Budget::new(), backlog: Default::default(), stale: false };
        out.history(&|_| Some(vec![]), (80, 24));
        out.history(&|_| None, (80, 24));
        assert_eq!(kinds(&sent.borrow()), [("history".into(), 0)], "the viewer hears it has all there is; no answer says nothing");
        assert_eq!(sent.borrow()[0]["data"], "");
    }

    #[test]
    fn a_history_burst_that_never_finds_room_is_lost_within_its_pace() {
        let ctl = Ctl::new(0);
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &|_| false, budget: Budget::new(), backlog: Default::default(), stale: false };
        let started = Instant::now();
        out.history(&|_| Some(vec![b'h'; 3 * CHUNK]), (80, 24));
        assert!(started.elapsed() < PACE + Duration::from_millis(500));
        assert!(out.stale && ctl.snapshot.load(SeqCst) && out.seq == 3, "lost: viewers skip to the next snapshot");
    }

    fn viewer_of(snapshot: &[u8], ticks: &[Vec<u8>], (cols, rows): (u16, u16)) -> vt100::Parser {
        let mut t = vt100::Parser::new(rows, cols, 1000);
        t.process(b"\x1b[?1049l\x1b[r\x1b[2J\x1b[H");
        t.process(snapshot);
        for b in ticks {
            t.process(b);
        }
        t
    }

    #[test]
    fn a_tick_sends_only_the_cells_an_erase_and_rewrite_changed() {
        let snap = b"\x1b[H\x1b[Jline one\r\nspinner 1";
        let mut r = Relayed::seed(snap, (40, 5));
        r.feed(b"\x1b[2;1H\x1b[2Kspinner 2");
        let tick = r.tick();
        assert!(!tick.is_empty() && tick.len() < 24, "{tick:?}");
        for clear in [&b"\x1b[J"[..], b"\x1b[2J", b"\x1b[2K"] {
            assert!(!tick.windows(clear.len()).any(|w| w == clear));
        }
        assert_eq!(viewer_of(snap, &[tick], (40, 5)).screen().contents(), "line one\nspinner 2");
        assert!(r.tick().is_empty());
    }

    #[test]
    fn rows_that_scroll_off_between_ticks_reach_the_viewers_scrollback_in_order() {
        let snap = b"a\r\nb\r\nc";
        let mut r = Relayed::seed(snap, (10, 3));
        r.feed(b"\r\nd\r\ne");
        let mut v = viewer_of(snap, &[r.tick()], (10, 3));
        assert_eq!(v.screen().contents(), "c\nd\ne");
        v.screen_mut().set_scrollback(2);
        assert_eq!(v.screen().rows(0, 10).next().unwrap(), "a");
        v.screen_mut().set_scrollback(1);
        assert_eq!(v.screen().rows(0, 10).next().unwrap(), "b");
    }

    #[test]
    fn entering_the_alternate_screen_in_a_tick_switches_the_viewers_too() {
        let snap = b"home";
        let mut r = Relayed::seed(snap, (10, 3));
        r.feed(b"\x1b[?1049h\x1b[HALT");
        let tick = r.tick();
        assert!(tick.starts_with(b"\x1b[?1049h"));
        let v = viewer_of(snap, &[tick], (10, 3));
        assert!(v.screen().alternate_screen());
        assert_eq!(v.screen().contents().trim(), "ALT");
    }

    #[test]
    fn leaving_the_alternate_screen_in_a_tick_switches_the_viewers_back() {
        let snap = b"home\x1b[?1049h\x1b[HALT";
        let mut r = Relayed::seed(snap, (10, 3));
        r.feed(b"\x1b[?1049l");
        let tick = r.tick();
        assert!(tick.starts_with(b"\x1b[?1049l"));
        let v = viewer_of(snap, &[tick], (10, 3));
        assert!(!v.screen().alternate_screen());
        assert_eq!(v.screen().contents().trim(), "home");
    }

    #[test]
    fn an_erase_of_the_lines_above_reaches_the_viewers_in_a_tick() {
        let snap = b"a\r\nb\r\nc\r\nd";
        let mut r = Relayed::seed(snap, (10, 3));
        r.feed(b"\x1b[3J");
        assert!(has(&r.tick(), b"\x1b[3J"));
    }

    /// Every frame `sent` drawn into one viewer's terminal, as the web viewer draws them.
    fn viewer(sent: &[Value], (cols, rows): (u16, u16)) -> vt100::Parser {
        let mut t = vt100::Parser::new(rows, cols, 1000);
        for f in sent {
            match f["kind"].as_str().unwrap() {
                "snapshot" => {
                    t.process(b"\x1b[?1049l\x1b[r\x1b[2J\x1b[H");
                    t.process(&data(f));
                }
                "output" => t.process(&data(f)),
                _ => {}
            }
        }
        t
    }

    #[test]
    fn the_relay_sends_only_the_cells_an_erase_and_rewrite_changed() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"\x1b[H\x1b[Jline one\r\nspinner 1"))).unwrap();
        tx.send((OUTPUT, with_offset(0, b"\x1b[2;1H\x1b[2Kspinner 2"))).unwrap();
        let _keep = tx;
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut Vec::<u8>::new(), ("sid", "gen"), (40, 5), &leased(Duration::from_millis(400)), &|f| { sent.borrow_mut().push(f); true },
            Budget::new(), &|_| None).unwrap();
        let sent = sent.into_inner();
        assert_eq!(kinds(&sent)[..2], [("snapshot".into(), 0), ("output".into(), 1)]);
        assert!(sent[2..].iter().all(|f| f["kind"] == "ping"));
        let change = data(&sent[1]);
        assert!(change.len() < 24, "{change:?}");
        for clear in [&b"\x1b[J"[..], b"\x1b[2J", b"\x1b[2K"] {
            assert!(!has(&change, clear), "{change:?}");
        }
        assert_eq!(viewer(&sent, (40, 5)).screen().contents(), "line one\nspinner 2");
    }

    #[test]
    fn rows_that_scroll_off_reach_the_viewers_scrollback_through_the_relay_in_order() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"a\r\nb\r\nc"))).unwrap();
        tx.send((OUTPUT, with_offset(0, b"\r\nd\r\ne"))).unwrap();
        let _keep = tx;
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut Vec::<u8>::new(), ("sid", "gen"), (10, 3), &leased(Duration::from_millis(300)), &|f| { sent.borrow_mut().push(f); true },
            Budget::new(), &|_| None).unwrap();
        let mut v = viewer(&sent.into_inner(), (10, 3));
        assert_eq!(v.screen().contents(), "c\nd\ne");
        v.screen_mut().set_scrollback(2);
        assert_eq!(v.screen().rows(0, 10).next().unwrap(), "a");
        v.screen_mut().set_scrollback(1);
        assert_eq!(v.screen().rows(0, 10).next().unwrap(), "b");
    }

    #[test]
    fn a_resize_sends_one_snapshot_at_the_new_size() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"screen"))).unwrap();
        tx.send((OUTPUT, with_offset(6, b"x"))).unwrap();
        tx.send((RESIZE, vec![0, 90, 0, 30])).unwrap();
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(7, b"fresh"))], asked: vec![] };
        let mut ctl = leased(Duration::from_millis(800));
        ctl.beat = Duration::from_millis(200);
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut holder, ("sid", "gen"), (120, 40), &ctl, &|f| { sent.borrow_mut().push(f); true }, Budget::new(), &|_| None).unwrap();
        let shape: Vec<(String, u64)> = sent.into_inner().iter().map(|f| (f["kind"].as_str().unwrap().into(), f["cols"].as_u64().unwrap())).collect();
        assert_eq!(holder.asked.len(), 1);
        assert_eq!(shape[..3], [("snapshot".into(), 120), ("output".into(), 120), ("snapshot".into(), 90)]);
        assert!(shape.len() > 3 && shape[3..].iter().all(|(kind, cols)| kind == "ping" && *cols == 90), "{shape:?}");
    }

    /// Feeds `reads` to a relay after its first screen, each `gap` after the last, and returns every
    /// frame it sent with when.
    fn relay_of(first: &[u8], reads: &[&[u8]], gap: Duration, lease: Duration) -> Vec<(Duration, Value)> {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, first))).unwrap();
        let reads: Vec<Vec<u8>> = reads.iter().map(|r| r.to_vec()).collect();
        let feeder = tx.clone();
        std::thread::spawn(move || {
            for read in reads {
                std::thread::sleep(gap);
                let _ = feeder.send((OUTPUT, with_offset(0, &read)));
            }
        });
        let _keep = tx;
        let sent = std::cell::RefCell::new(vec![]);
        let started = Instant::now();
        pump(&rx, &mut Vec::<u8>::new(), ("sid", "gen"), (40, 5), &leased(lease), &|f| { sent.borrow_mut().push((started.elapsed(), f)); true },
            Budget::new(), &|_| None).unwrap();
        sent.into_inner()
    }

    /// Every screen a viewer of `sent` showed, one per frame.
    fn screens_of(sent: &[(Duration, Value)]) -> Vec<String> {
        let frames: Vec<Value> = sent.iter().map(|(_, f)| f.clone()).collect();
        (1..=frames.len()).map(|n| viewer(&frames[..n], (40, 5)).screen().contents()).collect()
    }

    /// Codex review of #4035: an erase in one read and its repaint in a later one (more than a tick
    /// apart) showed every viewer the blank screen in between. (Time is given, not waited for.)
    #[test]
    fn an_erase_waits_for_its_repaint_and_goes_out_after_its_cap_without_one() {
        let snap = b"line one\r\nline two";
        let mut r = Relayed::seed(snap, (40, 5));
        r.feed(b"\x1b[H\x1b[2J");
        let t = Instant::now();
        assert!(!r.ready(t) && !r.ready(t + ERASE_CAP - Duration::from_millis(1)), "the erase waits");
        r.feed(b"line one\r\nline TWO");
        assert!(r.ready(t + Duration::from_millis(1)), "the repaint goes at once");
        assert_eq!(viewer_of(snap, &[r.tick()], (40, 5)).screen().contents(), "line one\nline TWO", "never the blank screen");
        // No repaint: the erase goes once its cap is over.
        r.feed(b"\x1b[H\x1b[2J");
        let t = Instant::now();
        assert!(!r.ready(t));
        assert!(r.ready(t + ERASE_CAP));
    }

    #[test]
    fn a_synchronized_update_split_across_reads_waits_for_its_end() {
        let snap = b"a\r\nb";
        let mut r = Relayed::seed(snap, (40, 5));
        r.feed(b"\x1b[?20");
        r.feed(b"26h\x1b[H\x1b[2Jhalf drawn and more");
        let t = Instant::now();
        assert!(!r.ready(t + Duration::from_millis(100)), "half drawn: held");
        r.feed(b"\x1b[H\x1b[2Jthe whole picture\x1b[?2026l");
        assert!(r.ready(t + Duration::from_millis(100)));
        assert_eq!(viewer_of(snap, &[r.tick()], (40, 5)).screen().contents(), "the whole picture");
    }

    /// Re-review of #4035: each new block restarted the cap, so back-to-back blocks held viewers for good.
    #[test]
    fn back_to_back_synchronized_updates_keep_the_cap_from_the_first() {
        let mut r = Relayed::seed(b"a", (40, 5));
        r.feed(b"\x1b[?2026hone");
        let t = Instant::now();
        std::thread::sleep(Duration::from_millis(20));
        r.feed(b"\x1b[?2026l\x1b[?2026htwo");
        assert!(r.ready(t + SYNC_CAP), "a tick by SYNC_CAP after the first block opened");
        let _ = r.tick();
        // The block still open is held again from that tick, and only from it.
        let t = Instant::now();
        r.feed(b"\x1b[?2026l\x1b[?2026hthree");
        assert!(!r.ready(t + Duration::from_millis(100)) && r.ready(t + SYNC_CAP));
    }

    /// The same through the relay: a block every 80 ms, each read ending inside the next one.
    #[test]
    fn a_relay_streaming_back_to_back_synchronized_updates_still_ticks() {
        let reads: Vec<Vec<u8>> = (0..10).map(|i| format!("\x1b[?2026l\x1b[?2026h\x1b[Hframe {i}").into_bytes()).collect();
        let reads: Vec<&[u8]> = reads.iter().map(Vec::as_slice).collect();
        let sent = relay_of(b"a", &reads, Duration::from_millis(80), Duration::from_millis(1000));
        let outputs: Vec<Duration> = sent.iter().filter(|(_, f)| f["kind"] == "output").map(|(at, _)| *at).collect();
        // (The first block opens at about 80 ms; a tick may be up to one TICK past its cap.)
        assert!(outputs.first().is_some_and(|at| *at < Duration::from_millis(80) + SYNC_CAP + Duration::from_millis(250)), "{outputs:?}");
        assert!(outputs.len() >= 2, "one about every SYNC_CAP: {outputs:?}");
    }

    #[test]
    fn a_synchronized_update_that_never_ends_still_goes_out_after_its_cap() {
        let sent = relay_of(b"a", &[b"\x1b[?2026hpartial"], Duration::from_millis(10), Duration::from_millis(700));
        let (at, _) = sent.iter().find(|(_, f)| f["kind"] == "output").expect("the update goes out");
        assert!(*at >= SYNC_CAP, "{at:?}");
        assert!(screens_of(&sent).last().unwrap().contains("partial"));
    }

    /// Review focus 3: a tick with no room in the relay queue waits and merges into the next one.
    /// Nothing queues up behind it, and nothing is lost (which used to cost a snapshot).
    #[test]
    fn ticks_with_no_room_merge_into_the_next_and_no_frame_is_lost() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(0, b"0"))).unwrap();
        // The relay queue has no room for output until the last of 20 changes is with the relay.
        let room = Arc::new(AtomicBool::new(false));
        let (feeder, opened) = (tx.clone(), room.clone());
        std::thread::spawn(move || {
            for i in 1..=20 {
                std::thread::sleep(Duration::from_millis(15));
                let _ = feeder.send((OUTPUT, with_offset(0, format!("\r\n{i}").as_bytes())));
            }
            opened.store(true, SeqCst);
        });
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(0, b"asked"))], asked: vec![] };
        let sent = std::cell::RefCell::new(vec![]);
        let send = |f: Value| (f["kind"] != "output" || room.load(SeqCst)) && { sent.borrow_mut().push(f); true };
        pump(&rx, &mut holder, ("sid", "gen"), (10, 5), &leased(Duration::from_millis(1000)), &send, Budget::new(), &|_| None).unwrap();
        let sent = sent.into_inner();
        assert!(holder.asked.is_empty(), "nothing was lost, so nothing is repaired");
        let drawn: Vec<Value> = sent.into_iter().filter(|f| f["kind"] != "ping").collect();
        assert_eq!(drawn.iter().map(|f| f["seq"].as_u64().unwrap()).collect::<Vec<_>>(), (0..drawn.len() as u64).collect::<Vec<_>>(), "no gap");
        assert_eq!(drawn[0]["kind"], "snapshot");
        assert!(drawn[1..].iter().all(|f| f["kind"] == "output" && !has(&data(f), b"\x1b[2J") && !has(&data(f), b"\x1b[J")), "the rest are diffs");
        assert!(drawn.len() - 1 < 20, "the waiting ticks merged: {} diffs for 20 changes", drawn.len() - 1);
        // The viewer is the terminal the holder drew: screen, and every row above it in order.
        let mut v = viewer(&drawn, (10, 5));
        assert_eq!(v.screen().contents(), "16\n17\n18\n19\n20");
        for back in 1..=16 {
            v.screen_mut().set_scrollback(back);
            assert_eq!(v.screen().rows(0, 10).next().unwrap(), (16 - back).to_string(), "{back} rows back");
        }
    }
}
