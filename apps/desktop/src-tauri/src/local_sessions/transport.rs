//! Dedicated paired-Mac socket. No OpenClaw node or gateway RPC is involved.
use super::engine_view::{self, EngineView};
use super::*;
use futures_util::{SinkExt, StreamExt};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    time::{Duration, Instant, SystemTime},
};
use tokio_tungstenite::{tungstenite::Message, MaybeTlsStream, WebSocketStream};

type Socket = WebSocketStream<MaybeTlsStream<tokio::net::TcpStream>>;
const ENDED_RETENTION_SECS: u64 = 86_400;

struct ClockAnchor {
    server: u64,
    round_trip: f64,
    at: Instant,
    wall: SystemTime,
}
impl ClockAnchor {
    fn now(&self) -> Option<u64> {
        deadline_now(
            self.server,
            self.round_trip,
            self.at.elapsed().as_secs_f64(),
            SystemTime::now()
                .duration_since(self.wall)
                .ok()
                .map(|d| d.as_secs_f64()),
        )
    }
}

/// What one poll tells `Host`: a fault only from the third failure in a row. A restarting engine
/// says nothing.
fn engine_answer(failures: &mut u32, read: Result<EngineView>) -> Option<Result<EngineView>> {
    *failures = if read.is_ok() { 0 } else { *failures + 1 };
    (read.is_ok() || *failures >= 3).then_some(read)
}

/// Polls the engine for as long as a `Host` watches. An aborted `run_once` drops its handle, which
/// does not stop this task: the `Host` going away does.
async fn poll_engine<F: std::future::Future<Output = Result<EngineView>>>(
    engine_tx: tokio::sync::watch::Sender<Option<Arc<EngineAnswer>>>,
    read: impl Fn() -> F,
) {
    let mut failures = 0;
    loop {
        if engine_tx.is_closed() {
            return;
        }
        let read_at = Instant::now();
        if let Some(answer) = engine_answer(&mut failures, read().await) {
            let _ = engine_tx.send(Some(Arc::new((read_at, answer))));
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

fn epoch_ms() -> u64 {
    SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64)
}

/// The one request outstanding on the socket. Single-flight, so the reply's type
/// plus this slot are what identify the frame being answered.
enum Rpc {
    Register {
        key: String,
        sent: Instant,
        wall: SystemTime,
        native_restriction: Option<String>,
        start_id: Option<String>,
    },
    Heartbeat {
        sent: Instant,
        wall: SystemTime,
        /// The sessions this frame carried: its ack's `presence` speaks for these alone.
        sids: Vec<String>,
    },
    Ended {
        key: String,
        /// Which end this frame reported. A resume and a second end can both land
        /// while the ACK is in flight, and the answer to the old end must never be
        /// read as an answer to the new one.
        at: u64,
        generation: String,
        sent: Instant,
    },
}

impl Rpc {
    fn sent(&self) -> Instant {
        match self {
            Rpc::Register { sent, .. } | Rpc::Heartbeat { sent, .. } | Rpc::Ended { sent, .. } => {
                *sent
            }
        }
    }
    /// The session an error frame carrying no `sessionId` belongs to. A device
    /// heartbeat names every session, so it names none.
    fn key(&self) -> Option<&str> {
        match self {
            Rpc::Register { key, .. } | Rpc::Ended { key, .. } => Some(key),
            Rpc::Heartbeat { .. } => None,
        }
    }
}

/// One `local.device.heartbeat` carries at most this many sessions. The relay
/// checks the envelope size before validating, so the byte budget bites first.
const HEARTBEAT_SESSIONS_MAX: usize = 24;

/// One discovery pass: the sessions, the holders born before this login session
/// (`holder::manager::stranded`), and the ones no terminal or panel is on
/// (`holder::manager::unattached`).
/// How long a held session nobody is on stays idle before it is stopped (`Host::stop_idle`): about
/// an hour, as Claude Code's own background sessions.
const IDLE_STOP_AFTER: Duration = Duration::from_secs(60 * 60);
/// The same while macOS is short of memory: Claude stops idle sessions sooner then too.
const IDLE_STOP_PRESSED: Duration = Duration::from_secs(20 * 60);

/// An idle stop is due: the session is idle (not working, not waiting on its person, not
/// unknown), nobody watches it, and it has been quiet long enough. `quiet`: None is not known.
fn idle_stop_due(status: &str, watched: bool, quiet: Option<Duration>, pressed: bool) -> bool {
    let after = if pressed { IDLE_STOP_PRESSED } else { IDLE_STOP_AFTER };
    status == "idle" && !watched && quiet.is_some_and(|quiet| quiet >= after)
}

/// How long since the session last did something: its transcript (Claude's JSONL, Codex's rollout)
/// was last written then. Never the time since it started.
fn quiet_for(mirror: &Mirror, now: SystemTime) -> Option<Duration> {
    let written = fs::metadata(mirror.native.transcript.as_ref()?).ok()?.modified().ok()?;
    now.duration_since(written).ok()
}

/// macOS says memory is short: `kern.memorystatus_vm_pressure_level` is warn (2) or critical (4).
#[cfg(target_os = "macos")]
fn memory_pressed() -> bool {
    let mut level: libc::c_int = 0;
    let mut len = std::mem::size_of::<libc::c_int>();
    let name = b"kern.memorystatus_vm_pressure_level\0";
    let read = unsafe {
        libc::sysctlbyname(name.as_ptr().cast(), (&mut level as *mut libc::c_int).cast(), &mut len, std::ptr::null_mut(), 0)
    };
    read == 0 && level > 1
}

#[cfg(not(target_os = "macos"))]
fn memory_pressed() -> bool {
    false
}

/// One discovery pass, and when its read started (epoch ms).
type Discovered = (Vec<NativeSession>, Vec<crate::holder::manager::HolderInfo>, Vec<crate::holder::manager::HolderInfo>, u64);

/// One poll's answer and when its read started.
type EngineAnswer = (Instant, Result<EngineView>);

pub(super) struct Host {
    workspace_at: Instant,
    workspace_key: Option<String>,
    dir: PathBuf,
    home: PathBuf,
    mirrors: BTreeMap<String, Mirror>,
    stop: Arc<AtomicBool>,
    /// Set by `supervise`, drained by `connection`. `None` in tests that never
    /// drive `connection` through `supervise`.
    hooks: Option<tokio::sync::mpsc::Receiver<super::lifecycle::HookEvent>>,
    /// The discovery wake `run_once` was given: a hook naming a session not yet mirrored runs it.
    rediscover: Option<Arc<tokio::sync::Notify>>,
    /// Sessions that have ended and whose end the relay has not been told about.
    ended_pending: Vec<String>,
    /// Whether the relay on this socket lists `endedStartId`; None until one of its acks has
    /// said. Per connection, like every feature: a reconnect can reach another relay version.
    relay_reads_ended_start: Option<bool>,
    /// The engine's latest `sessions` answer (`run_once`'s poller); None until it has one.
    engine: Option<tokio::sync::watch::Receiver<Option<Arc<EngineAnswer>>>>,
    /// When `observe` last ran: an answer read before it may not know the mirrors it made.
    observed_at: Option<Instant>,
    /// When the pass being observed started its read (epoch ms); None in tests that call `observe`.
    discovery_read_ms: Option<u64>,
    /// Claude mirrors the last applied answer had no live row for: a daemon respawn leaves a gap
    /// of one poll, so the second miss in a row is the end.
    engine_missed: BTreeSet<String>,
    /// Claude mirrors whose person left on purpose (`SessionEnd`): the engine's end does not say so.
    left_on_purpose: BTreeSet<String>,
    /// Live Claude rows that have woken discovery: `(conversation, pid, birth)`, so a row that
    /// never captures (a spare, a non-UUID id) wakes it once.
    engine_woken: BTreeSet<(String, u32, u64)>,
    /// Sessions whose heartbeat fields moved between heartbeats: one goes out
    /// early rather than up to ten seconds late.
    heartbeat_dirty: BTreeSet<String>,
    /// The device heartbeat cadence. A field so tests can drive many ticks fast.
    heartbeat_interval: Duration,
    /// Proves a native session is still the process it was. A field so a test can make it slow.
    verify: Verify,
    /// A test's answer for Settings → "Share every new session"; None reads the setting.
    share_every: Option<bool>,
}

/// What this Mac's own doors (the band, the share skill) decided for a session, by mirror key,
/// as `(shared, stop_owed)`, until the relay loop applies it (`Host::share_tick`): only that
/// loop writes a mirror.
static SHARE_ASKS: std::sync::Mutex<BTreeMap<String, (bool, bool)>> = std::sync::Mutex::new(BTreeMap::new());

/// This Mac's person shares the session of mirror `key`, or stops sharing it. A stop is owed to
/// the workspace until `stop_settled`.
pub(super) fn ask_share(key: &str, shared: bool) {
    if let Ok(mut asks) = SHARE_ASKS.lock() {
        asks.insert(key.to_owned(), (shared, !shared));
    }
}

/// Mirrors whose owed stop the workspace took (the server pause succeeded), until the relay
/// loop records it. Its own set: it must not replace a stop or a share that waits in `SHARE_ASKS`.
static STOPS_SETTLED: std::sync::Mutex<BTreeSet<String>> = std::sync::Mutex::new(BTreeSet::new());

pub(super) fn stop_settled(key: &str) {
    if let Ok(mut settled) = STOPS_SETTLED.lock() {
        settled.insert(key.to_owned());
    }
}

/// When this Mac's person last stopped sharing each mirror. The server's presence can still say
/// "shared" for a moment after (read before the pause, delivered after it).
// ponytail: a fixed grace, not an ordering of the two clocks; compare presence `at` with the
// pause's own server time if a late "shared" ever undoes a stop.
static STOPPED_AT: std::sync::Mutex<BTreeMap<String, Instant>> = std::sync::Mutex::new(BTreeMap::new());
const STOP_GRACE: Duration = Duration::from_secs(30);

pub(super) fn is_settled(key: &str) -> bool {
    STOPS_SETTLED.lock().is_ok_and(|settled| settled.contains(key))
}

/// What was decided for mirror `key` and is not applied yet, as `(shared, stop_owed)`: a reader
/// of the stored mirror answers with this, not with the moment before.
pub(super) fn asked(key: &str) -> Option<(bool, bool)> {
    SHARE_ASKS.lock().ok()?.get(key).copied()
}

/// The one answer to "does this session send or take anything right now", for every exit and
/// every reader of a mirror, in the relay loop or outside it: withheld as stored, or its person
/// pressed stop and the loop has not applied that yet.
pub(super) fn stopped(key: &str, mirror: &Mirror) -> bool {
    asked(key).is_some_and(|(shared, _)| !shared) || mirror.withheld()
}

/// Shared once and then stopped (Only me) on this Mac: it still registers and heartbeats, as
/// `shared: false`, so it stays in its owner's own list as "Only you" while it runs. Nothing of
/// it is uploaded and it takes nothing. A session never shared registers nothing (2026-10-26).
fn paused(mirror: &Mirror) -> bool {
    mirror.withheld() && mirror.session_id.is_some()
}

type Verify = fn(PathBuf, NativeSession) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<NativeSession>> + Send>>;

/// A reconciliation the journal alone cannot answer: the native process has to be checked
/// (`lsof`, a provider call, transcript reads -- seconds each, 12-40 s on a loaded Mac).
struct Ask {
    session: String,
    request: String,
    generation: String,
    receipt: Receipt,
    /// A Codex session and the turn whose end settles the command.
    turn: Option<(NativeSession, String)>,
}

/// How long an answer of `unknown` stands before the relay's next ask is checked again.
const RECONCILE_UNKNOWN_EVERY: Duration = Duration::from_secs(60);

/// The connection loop's side of `reconcile_worker`. The loop is one serial task: awaiting a
/// check in it held back every heartbeat and every frame read (a Mac "offline" three times in
/// seven minutes, 2026-10-07), so checks run in the worker and their receipts come back here.
struct Reconciles {
    asks: tokio::sync::mpsc::UnboundedSender<Ask>,
    /// (session, request) -> None while its check runs, else when it last answered `unknown`.
    state: BTreeMap<(String, String), Option<Instant>>,
}

impl Reconciles {
    fn ask(&mut self, ask: Ask) {
        let key = (ask.session.clone(), ask.request.clone());
        match self.state.get(&key) {
            Some(None) => return,
            Some(Some(at)) if at.elapsed() < RECONCILE_UNKNOWN_EVERY => return,
            _ => {}
        }
        if self.asks.send(ask).is_ok() {
            self.state.insert(key, None);
        }
    }

    fn answered(&mut self, ask: &Ask, unknown: bool) {
        let key = (ask.session.clone(), ask.request.clone());
        if unknown {
            self.state.insert(key, Some(Instant::now()));
        } else {
            self.state.remove(&key);
        }
    }
}

/// One check at a time, off the connection loop. Ends with its connection (the channels close).
async fn reconcile_worker(
    dir: PathBuf,
    home: PathBuf,
    verify: Verify,
    mut asks: tokio::sync::mpsc::UnboundedReceiver<Ask>,
    answers: tokio::sync::mpsc::UnboundedSender<(Ask, Result<Receipt>)>,
) {
    while let Some(ask) = asks.recv().await {
        let answer = settle_unknown(&dir, &home, verify, &ask).await;
        if answers.send((ask, answer)).is_err() {
            return;
        }
    }
}

/// The slow half of a reconciliation: was the send admitted (Claude), did its turn end (Codex).
async fn settle_unknown(dir: &Path, home: &Path, verify: Verify, ask: &Ask) -> Result<Receipt> {
    let journal = Journal { dir: dir.join("receipts") };
    let mut receipt = ask.receipt.clone();
    if let Some(ingress) = receipt.ingress.clone().filter(|i| i.native.provider == "claude") {
        if verify(home.into(), ingress.native.clone()).await.is_ok() {
            let home = home.to_path_buf();
            // A synchronous transcript scan: never on a runtime worker the loop may share.
            let admitted = tokio::task::spawn_blocking(move || claude::admission(&home, &ingress))
                .await
                .map_err(|_| "admission check stopped")?;
            if let Ok(Some(id)) = admitted {
                receipt = journal.finish(&ask.session, &ask.request, Some(id), false)?;
            }
        }
    }
    if receipt.outcome == "unknown" && !receipt.settled {
        if let Some((native, turn)) = ask.turn.clone() {
            if verify(home.into(), native.clone()).await.is_ok() {
                let finished = tokio::task::spawn_blocking(move || {
                    native.transcript.as_ref().is_some_and(|path| codex::turn_finished(path, &turn))
                })
                .await
                .map_err(|_| "turn check stopped")?;
                if finished {
                    receipt = journal.settle(&ask.session, &ask.request)?;
                }
            }
        }
    }
    Ok(receipt)
}

/// How long a per-session verdict (a stale generation, a relay rejection) keeps that session out
/// of rotation. Connections are long-lived now, so "for this connection" could mean hours; this
/// bounds it and lets a session that has since been repaired re-register on the same socket.
const QUARANTINE: Duration = Duration::from_secs(300);

/// A Mac with nothing running still checks in this often. Resume requests ride the heartbeat
/// acknowledgement, and an idle (or just-woken) Mac is the usual one to resume on.
const IDLE_HEARTBEAT: Duration = Duration::from_secs(30);

/// How long a register, heartbeat or end waits for its acknowledgment before the connection is
/// given up. Well past the ws-message Lambda's 10 s: the backend goes on working after the Lambda
/// gives up and still posts the ack (15.6 s after the frame, prod 2026-10-07). At 10 s the Mac
/// dropped a healthy connection and every session registered again. The relay's lease is 30 s.
const ACK_DEADLINE: Duration = Duration::from_secs(30);

fn idle_heartbeat() -> Value {
    json!({"type":"local.device.heartbeat","sessions":[]})
}

/// A connection that lived long enough was healthy: start over at one second. A short-lived one
/// (connect-then-die, the 2026-09-12 loop) doubles the wait, capped at 30 s.
fn reconnect_delay(previous: u64, lived: Duration) -> u64 {
    if lived >= Duration::from_secs(30) {
        1
    } else {
        (previous * 2).clamp(1, 30)
    }
}

/// Mirrors `reconnect_delay` for the supervisor's own restart backoff: a `run_once` call that
/// lived at least 60 s was a healthy stretch overall, so start the next restart wait over at
/// one second instead of wherever the backoff had climbed to.
fn restart_delay(previous: u64, lived: Duration) -> u64 {
    if lived >= Duration::from_secs(60) {
        1
    } else {
        (previous * 2).clamp(1, 60)
    }
}

static SCAN_IN_FLIGHT: AtomicBool = AtomicBool::new(false);

/// Held by the task doing a workspace scan; released when that task ends, not when its caller
/// stops waiting.
struct ScanClaim(&'static AtomicBool);
impl ScanClaim {
    fn take(flag: &'static AtomicBool) -> Option<Self> {
        flag.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .ok()
            .map(|_| Self(flag))
    }
}
impl Drop for ScanClaim {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

impl Host {
    async fn observe_workspace(&mut self, key: &str) -> Result<()> {
        let native = self
            .mirrors
            .get(key)
            .ok_or("missing local session")?
            .native
            .clone();
        // lsof and git are async and 2 s-bounded each; the file reads run on the blocking pool.
        // The whole observation is bounded too: an unknown result costs one advisory, a stall
        // costs every frame queued behind this loop.
        // One scan at a time: a timed-out scan stuck on a stalled filesystem is only detached,
        // so its claim is held until it really ends and later observations read as unknown.
        let observed = match ScanClaim::take(&SCAN_IN_FLIGHT) {
            None => Err("workspace observation in progress".into()),
            Some(claim) => {
                let home = self.home.clone();
                let scan = tokio::spawn(async move {
                    let _claim = claim;
                    match workspace::session_root(&native, &home).await {
                        Ok(cwd) => workspace::scan(&cwd, &home).await,
                        Err(error) => Err(error),
                    }
                });
                match tokio::time::timeout(Duration::from_secs(15), scan).await {
                    Ok(joined) => joined.map_err(|_| "workspace scan stopped")?,
                    Err(_) => Err("workspace observation timed out".into()),
                }
            }
        };
        let root = observed.as_ref().ok().map(|(root, _)| root.clone());
        let keys: Vec<_> = self
            .mirrors
            .iter()
            .filter(|(k, m)| {
                *k == key
                    || (root.is_some()
                        && m.workspace.root() == root.as_deref()
                        && m.ended_at.is_none())
            })
            .map(|(k, _)| k.clone())
            .collect();
        let overlap = keys.len() > 1;
        for key in keys {
            let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
            let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
            mirror.workspace.observe(observed.clone(), overlap);
            persist_changed(&self.dir, &key, mirror, &before)?;
        }
        Ok(())
    }
    fn begin_registration(&mut self, key: &str) -> Result<Value> {
        self.begin_registration_with_status(key, None, false)
    }
    fn begin_ended_recovery(&mut self, key: &str) -> Result<Value> {
        self.begin_registration_with_status(key, Some("idle"), true)
    }
    fn begin_registration_with_status(
        &mut self,
        key: &str,
        status: Option<&str>,
        recovering_ended: bool,
    ) -> Result<Value> {
        let origin = self.mirrors.get(key).and_then(|m| origin_session(&self.mirrors, &m.native));
        let mirror = self.mirrors.get_mut(key).ok_or("missing local session")?;
        let reset_end = recovering_ended && mirror.ended_sent;
        if recovering_ended {
            mirror.ended_sent = false;
        }
        if !mirror.registration.pending || mirror.dirty || reset_end {
            mirror.registration.pending = true;
            // Durable before the first send: retry this exact generation/CAS
            // predecessor until ACK, even when discovery has already found C. A generation the
            // relay already acknowledged risks nothing on a lost write, so a full disk never keeps
            // a reconnect's sessions off the relay.
            let path = self.dir.join(format!("{key}.json"));
            if !reset_end && mirror.registration.acknowledged.as_ref() == Some(&mirror.generation) {
                mirror.persist(&path)?;
            } else {
                mirror.persist_durable(&path)?;
            }
        }
        mirror.registration.sent_withheld = mirror.withheld();
        let mut frame = registration(mirror);
        if let Some(origin) = origin {
            frame["session"]["forkedFrom"] = json!(origin);
        }
        // An ended mirror registers only to drain, as `runtime-unverified`. The relay refuses a
        // start identity on an unverified register (LOCAL_SESSION_CONFLICT, every retry), and
        // keeps the one this generation already bound.
        if status.is_none() {
            if let Some(start_id) = start_links::linked(&self.dir, &mirror.native, &mirror.generation) {
                frame["session"]["startId"] = json!(start_id);
            }
        }
        if let Some(status) = status {
            frame["session"]["status"] = json!(status);
            frame["session"]["activeTurnId"] = Value::Null;
            frame["session"]["controlUnavailableReason"] = json!("runtime-unverified");
        }
        Ok(frame)
    }
    fn observe(&mut self, update: Vec<NativeSession>) -> Result<Vec<String>> {
        self.observed_at = Some(Instant::now());
        // ← moves a Codex thread on as a fork. Its original is no longer listed while its process
        // stays, so its pid check never ends it. A listed fork naming an unlisted original does; a
        // listing gap alone never ends anything. Claude's is the engine's (`moved-on`).
        let moved: Vec<String> = update
            .iter()
            .filter(|fork| fork.provider != "claude")
            .filter_map(|fork| {
                let origin = fork.forked_from.as_deref()?;
                let same = |provider: &str, profile: &str, conversation: &str| {
                    provider == fork.provider && profile == fork.profile && conversation == origin
                };
                if update.iter().any(|s| same(&s.provider, &s.profile, &s.conversation)) {
                    return None;
                }
                self.mirrors
                    .iter()
                    .find(|(_, m)| m.ended_at.is_none() && same(&m.native.provider, &m.native.profile, &m.native.conversation))
                    .map(|(key, _)| key.clone())
            })
            .collect();
        // What this pass lists: `reconciled` below also holds every mirror it does not.
        let listed_claude: BTreeSet<String> = update.iter().filter(|s| s.provider == "claude").map(|s| s.conversation.clone()).collect();
        let mut invalidated = vec![];
        let mut heartbeat = vec![];
        let mut reconciled = self
            .mirrors
            .values()
            .map(|m| {
                let mut native = m.registration.successor.as_ref().unwrap_or(&m.native).clone();
                // A Codex app server keeps running after it lets a thread's rollout go, so
                // discovery stops naming the thread while its pid lives on. The rollout still
                // says whether a turn runs; a hook's waiting prompt is left alone.
                if native.provider == "codex" && m.ended_at.is_none() && native.status != "waiting-approval" {
                    if let Some(path) = native.transcript.as_deref() {
                        native.status = codex::turn_status(path).into();
                    }
                }
                // A live session is online unless something says it works or waits on its
                // person (Happy's rule). The restart's "unknown" is only cleared by a hook or a
                // listing, and an idle session gets neither: a Codex thread's rollout can be gone.
                // A hook still moves it to busy or waiting when it next works. Not while a prompt
                // is open and unresolved (`waiting`, e.g. a hold whose delivery failed): the
                // terminal may still be blocked on it, so it stays unknown until the provider says
                // otherwise. Not Claude: its "unknown" is the engine's to clear (`apply_engine`).
                if native.provider != "claude"
                    && m.ended_at.is_none()
                    && native.status == "unknown"
                    && m.waiting.is_none()
                    && still_running(&native.process)
                {
                    native.status = "idle".into();
                }
                native
            })
            .collect();
        reconcile_sessions(&mut reconciled, update);
        // (Also here, so an ask is applied while the relay is unreachable.)
        invalidated.extend(self.share_tick());
        let scope = self.dir.clone();
        for mut native in reconciled {
            let key = native_key(&native);
            // A continuation is the same conversation under a new id: its earlier ids are its
            // origin's, then the origin's own.
            if let Some(origin) = native.forked_from.clone() {
                let mut chain = self
                    .mirrors
                    .values()
                    .find(|m| m.native.provider == native.provider && m.native.profile == native.profile && m.native.conversation == origin)
                    .map(|m| m.native.aliases.clone())
                    .unwrap_or_default();
                chain.retain(|id| *id != origin && *id != native.conversation);
                chain.push(origin);
                native.aliases = chain;
            }
            // Started with Lightbulb (it has a holder: `lightbulb claude|codex`, New session, a
            // remote start, a resume, a pick-up), or continued by its person (←, `/background`) out
            // of a shared session they used: shared from its start. Not when a peer's message or
            // another agent moved it on (Claude backgrounds an idle terminal to answer a peer):
            // that job was listed as a session nobody started (2026-10-26). Not out of one that
            // never had a prompt (no transcript) either.
            let ours = native.holder.is_some()
                || native.forked_by_person == Some(true) && native.forked_from.as_deref().is_some_and(|origin| {
                    self.mirrors.values().any(|m| {
                        m.shared
                            && m.native.transcript.is_some()
                            && m.native.provider == native.provider
                            && m.native.conversation == origin
                    })
                });
            let before = self
                .mirrors
                .get(&key)
                .map(serde_json::to_value)
                .transpose()
                .map_err(|e| e.to_string())?;
            let generation = native.process.generation();
            let mut invalidate = before.is_none();
            let mut revived = false;
            if let Some(mirror) = self.mirrors.get_mut(&key) {
                // Its holder or its lineage was found after its mirror was made. Never for a
                // session the relay has heard of: its person stopped sharing it, and stays asked.
                // Except a continuation: it stays private until its own first turn says who moved
                // it on (an old human turn in its origin says nothing about a peer's background),
                // and its person's turn shares it then, unless they stopped sharing it.
                let decided = mirror.native.forked_by_person.is_none() && native.forked_by_person == Some(true);
                let stopped = STOPPED_AT.lock().is_ok_and(|s| s.contains_key(&key));
                if ours && !mirror.shared && (mirror.session_id.is_none() || decided && !stopped) {
                    mirror.shared = true;
                    // A private register in flight, or one the relay holds: register again.
                    if mirror.registration.pending || mirror.session_id.is_some() {
                        invalidate = true;
                    }
                }
                if mirror.ended_at.is_some() {
                    // An end is authoritative unless a new process resumes the conversation,
                    // or fresh native evidence below proves the old end was false. A generation
                    // minted by an older formula alone is not a resume: that would register an
                    // "ended" status the relay rejects (INVALID_REQUEST).
                    let resumed_codex_activity = mirror.ended_reason.as_deref() == Some("exited")
                        && native.provider == "codex"
                        && mirror.ended_at.is_some_and(|ended_at| {
                            native.transcript.as_deref().is_some_and(|path| {
                                codex::active_turn_started_after(path, ended_at, mirror.ended_at_ms)
                            })
                        });
                    // A Claude row in this pass is one the engine lists live: a person who went
                    // to the agent view and came back, or an end the engine took back. Not an end
                    // someone meant: its process may outlive it for a moment.
                    let returned_claude = native.provider == "claude"
                        && listed_claude.contains(&native.conversation)
                        && !super::session_states::end_was_meant(mirror)
                        // A pass that read before the end says nothing about it. A true false end
                        // loses nothing: the returning row wakes a fresh pass (`apply_engine`).
                        && self.discovery_read_ms.is_none_or(|read| mirror.ended_at_ms.is_none_or(|end| read >= end));
                    // Older releases ended an idle resumed Codex terminal after its history
                    // drained. Its provider and held terminal still run, but no new task can
                    // start while the UI hides that terminal behind Continue. A fresh native
                    // capture proves it is input-ready; repair that false end without a new writer.
                    let returned_codex_terminal = native.provider == "codex"
                        && native.status == "idle"
                        && native.holder.is_some()
                        && !super::session_states::end_was_meant(mirror)
                        && self.discovery_read_ms.is_none_or(|read| mirror.ended_at_ms.is_none_or(|end| read >= end));
                    // A Codex thread that is idle again and not shared stays ended: reviving it
                    // only defers its end behind an upload and a register it never makes.
                    if native.provider == "codex" && native.status == "idle" && !mirror.shared {
                        continue;
                    }
                    if mirror.native.process == native.process {
                        if !resumed_codex_activity && !returned_claude && !returned_codex_terminal {
                            continue;
                        }
                        let next_generation = uuid::Uuid::new_v4().to_string();
                        if mirror.registration.pending
                            || mirror.pending.is_some()
                            || !mirror.queue.is_empty()
                        {
                            // Finish any old-generation register or event batch before
                            // promoting the resume generation.
                            mirror.resume_generation = Some(next_generation);
                        } else {
                            // Fence an end frame already in flight; the relay accepts
                            // this generation because previousGeneration names the old one.
                            mirror.previous_generation = Some(mirror.generation.clone());
                            mirror.generation = next_generation;
                            mirror.resume_registration_pending = true;
                        }
                    } else if mirror.generation == generation {
                        continue;
                    }
                    mirror.ended_at = None;
                    mirror.ended_at_ms = None;
                    mirror.ended_reason = None;
                    // It runs again: an End asked of the run before says nothing about this one's end.
                    if let Some(session) = &mirror.session_id {
                        crate::holder::manager::end_asked_forget(session);
                    }
                    if native.provider == "codex" && native.status == "idle" && native.holder.is_none() {
                        // An unheld thread's turn completed between discovery sweeps: upload it,
                        // then finish it. A held terminal is still open and waiting for input.
                        mirror.deferred_end_reason = Some("exited".into());
                    }
                    // Its next end is news again: leaving this set would make the
                    // resumed session's own end unreportable forever.
                    mirror.ended_sent = false;
                    invalidate = true;
                    revived = true;
                }
                if mirror.registration.acknowledged.is_none()
                    && mirror.session_id.is_some()
                    && !mirror.registration.pending
                {
                    mirror.registration.acknowledged = Some(
                        mirror
                            .previous_generation
                            .as_ref()
                            .unwrap_or(&mirror.generation)
                            .clone(),
                    );
                }
                if mirror.native.restriction.as_deref() == Some("history-gap")
                    && !(mirror.native.process == native.process
                        && native.restriction.is_none()
                        && readable_history(mirror))
                {
                    native.restriction = Some("history-gap".into());
                }
                // A process's lineage never changes: one failed `ps` (or a spare-claimed job
                // whose origin terminal has since closed) must not drop it, or the relay loses
                // `forkedFrom` on the next registration and lists the origin beside its fork.
                if native.forked_from.is_none() && mirror.native.process == native.process {
                    native.forked_from.clone_from(&mirror.native.forked_from);
                }
                if native.forked_by_person.is_none() && mirror.native.process == native.process {
                    native.forked_by_person = mirror.native.forked_by_person;
                }
                // Nor its earlier ids, once its origin's mirror is reaped.
                if mirror.native.process == native.process && mirror.native.aliases.len() > native.aliases.len() {
                    native.aliases.clone_from(&mirror.native.aliases);
                }
                // Discovery cannot read the last assistant message; only a Stop hook
                // carries it, so a listing must not blank it.
                if native.last_assistant_message.is_none() {
                    native
                        .last_assistant_message
                        .clone_from(&mirror.native.last_assistant_message);
                }
                // Discovery sees a running Claude process as busy even while its
                // PermissionRequest hook is holding a prompt. Keep the hook's
                // stronger status until that exact hold is released.
                // (Not a daemon hold: Codex's app-server status is the truth, and `codex_hold`
                // follows it.)
                if mirror.native.process == native.process
                    && matches!(native.status.as_str(), "busy" | "idle" | "waiting-approval")
                    && mirror.waiting.as_ref().and_then(|prompt| prompt.held.as_ref()).is_some_and(|held| !daemon_held(held))
                {
                    native.status = "waiting-approval".into();
                }
                // The same process keeps its stored generation, whatever formula minted
                // it: an upgrade that changes the fingerprint must not re-register it.
                if mirror.generation != generation && mirror.native.process != native.process {
                    invalidate = true;
                    // The predecessor is not running; the session is, as the successor, and
                    // discovery just read its status. Control stays unverified until `advance`,
                    // but the status must not wait on that: `advance` first drains the old
                    // generation's history, which a relay that never acks holds up forever.
                    mirror.native.status.clone_from(&native.status);
                    mirror.registration.successor = Some(native);
                    if mirror.native.restriction.as_deref() != Some("history-gap") {
                        mirror.native.restriction = Some("runtime-unverified".into());
                    }
                    mirror.advance();
                } else {
                    mirror.registration.successor = None;
                    mirror.native = native;
                }
            } else {
                if self.live_count() >= 512 {
                    return Err("local session capacity reached".into());
                }
                let shared = ours || self.share_every.unwrap_or_else(|| crate::shell_integration::is_on(&self.home));
                // Known from its first frame: without it the row shows twice until a heartbeat joins it.
                let network_id = self.engine.as_ref().and_then(|engine| engine.borrow().clone()).and_then(|answer| {
                    answer.1.as_ref().ok()?.key(&native.provider, &native.conversation).map(str::to_owned)
                });
                self.mirrors.insert(
                    key.clone(),
                    Mirror {
                        workspace: workspace::State::default(),
                        dirty: false,
                        native,
                        generation,
                        previous_generation: None,
                        session_id: None,
                        cursor: Cursor::default(),
                        after_seq: 0,
                        queue: vec![],
                        pending: None,
                        registration: RegistrationState::default(),
                        ended_at: None,
                        ended_at_ms: None,
                        resume_generation: None,
                        resume_registration_pending: false,
                        deferred_end_reason: None,
                        ended_reason: None,
                        ended_sent: false,
                        shared,
                        stop_owed: false,
                        waiting: None,
                        stats: Default::default(),
                        subagents: Default::default(),
                        network_id,
                    },
                );
            }
            if revived {
                // Its end is no longer news; whatever has not been sent must not be.
                self.ended_pending.retain(|pending| pending != &key);
            }
            let withheld = self.stopped_now(&key);
            let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
            codex_hold(mirror, withheld);
            if start_links::bind(&scope, &mirror.native, &mirror.generation)? {
                invalidate = true;
            }
            let _ = capture_batch(mirror);
            let after = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
            if before.as_ref().is_some_and(|b| !b["native"]["restriction"].is_null())
                && mirror.native.restriction.is_none()
            {
                // A heartbeat can revoke control, but only registration restores it.
                invalidate = true;
            }
            if before.as_ref().is_some_and(|b| b["native"]["forked_from"] != after["native"]["forked_from"]) {
                // Lineage found late (a failed `ps`): only registration carries `forkedFrom`.
                invalidate = true;
            }
            if before.as_ref() != Some(&after) || mirror.dirty {
                if invalidate {
                    invalidated.push(key.clone());
                } else if before.as_ref().is_some_and(|b| b["native"] != after["native"]) {
                    // Status, title, turn or cwd moved. The relay learns that from the
                    // next heartbeat; re-registering a live session would be churn.
                    heartbeat.push(key.clone());
                }
                mirror.persist(&self.dir.join(format!("{key}.json")))?;
            }
        }
        self.heartbeat_dirty.extend(heartbeat);
        for key in moved {
            self.end_session(&key, "exited")?;
        }
        Ok(invalidated)
    }

    /// Applies what this Mac's person asked (`ask_share`). Stopping takes effect here: from this
    /// call the mirror uploads, registers and heartbeats nothing, takes no command or typed
    /// input, and its terminal relay is stopped. It keeps its cursor and its queue, so sharing
    /// it again goes on from there and the workspace has the whole session. Returns the keys
    /// shared now: the relay may have let them lapse, so they register again.
    fn share_tick(&mut self) -> Vec<String> {
        let mut shared_now = vec![];
        let Ok(mut asks) = SHARE_ASKS.lock() else { return shared_now };
        // (An ask for a mirror this loop does not hold stays: it is not this loop's to drop.)
        asks.retain(|key, (shared, owed)| {
            let Some(mirror) = self.mirrors.get_mut(key) else { return true };
            let changed = mirror.shared != *shared;
            if changed || mirror.stop_owed != *owed {
                mirror.shared = *shared;
                mirror.stop_owed = *owed;
                // (A failed write leaves it dirty: RAM is the truth and the next persist stores it.)
                let _ = mirror.persist(&self.dir.join(format!("{key}.json")));
            }
            if changed {
                relay_log(&format!("{} {}", short(key), if *shared { "shared by its person" } else { "no longer shared" }));
                match (&mirror.session_id, *shared) {
                    (Some(session), false) => super::screen::revoke(session),
                    (_, true) => shared_now.push(key.clone()),
                    _ => {}
                }
                if let Ok(mut stopped) = STOPPED_AT.lock() {
                    if *shared { stopped.remove(key) } else { stopped.insert(key.clone(), Instant::now()) };
                }
            }
            false
        });
        drop(asks);
        // After the asks: a stop and its settlement can arrive in the same turn. One that was
        // shared again meanwhile owes nothing.
        if let Ok(mut settled) = STOPS_SETTLED.lock() {
            settled.retain(|key| {
                let Some(mirror) = self.mirrors.get_mut(key) else { return true };
                if mirror.stop_owed {
                    mirror.stop_owed = false;
                    let _ = mirror.persist(&self.dir.join(format!("{key}.json")));
                }
                false
            });
        }
        shared_now
    }

    /// The workspace's own switch for a session (the web's sharing control) is the same switch as
    /// the band's: a pause the server reports stops sharing here, and its end shares again.
    /// `entry`: one session's presence (a frame, or an entry of a heartbeat ack). Not while a
    /// decision of this Mac's own is still owed to the server or waits to be applied: the
    /// server has not heard of it yet. Returns the key when the session is shared now.
    fn follow_presence(&mut self, entry: &Value) -> Option<String> {
        let paused = entry["paused"].as_bool()?;
        let (key, mirror) = self.mirrors.iter_mut().find(|(_, m)| {
            m.ended_at.is_none()
                && m.session_id.is_some()
                && m.session_id.as_deref() == entry["sessionId"].as_str()
                && entry["generation"].as_str() == Some(m.registration.acknowledged.as_deref().unwrap_or(&m.generation))
        })?;
        if mirror.shared != paused || mirror.stop_owed || asked(key).is_some() {
            return None;
        }
        if !paused && STOPPED_AT.lock().is_ok_and(|stopped| stopped.get(key).is_some_and(|at| at.elapsed() < STOP_GRACE)) {
            return None;
        }
        mirror.shared = !paused;
        let _ = mirror.persist(&self.dir.join(format!("{key}.json")));
        relay_log(&format!("{} {} in the workspace", short(key), if paused { "paused" } else { "shared again" }));
        if paused {
            if let Some(session) = &mirror.session_id {
                super::screen::revoke(session);
            }
            return None;
        }
        Some(key.clone())
    }

    /// The last moment before a side effect for mirror `key` (a command, an approval, an
    /// upload), after whatever this turn awaited: withheld, or its person pressed stop since
    /// this turn began and the loop has not applied it yet.
    fn stopped_now(&self, key: &str) -> bool {
        self.mirrors.get(key).is_none_or(|mirror| stopped(key, mirror))
    }

    /// Whether a relay frame names a session that is stopped (a terminal frame queued before
    /// sharing stopped, `watch`, typed `input`): it is dropped. A stop the loop has not applied
    /// yet counts, as at every other exit.
    fn refuses_frame(&self, body: &Value) -> bool {
        self.mirrors
            .iter()
            .find(|(_, m)| m.session_id.is_some() && m.session_id.as_deref() == body["sessionId"].as_str())
            .is_some_and(|(key, _)| self.stopped_now(key))
    }

    fn live_count(&self) -> usize {
        self.mirrors
            .values()
            .filter(|m| m.ended_at.is_none())
            .count()
    }

    fn reset_relay_features(&mut self) {
        super::session_states::read_features(&Value::Null);
        self.relay_reads_ended_start = None;
        for mirror in self.mirrors.values_mut() {
            mirror.subagents.relay = false;
            mirror.subagents.relay_known = false;
        }
    }

    fn refresh_ended_recovery(&mut self, recovering_ended: &mut BTreeSet<String>) {
        recovering_ended.retain(|key| {
            self.mirrors
                .get(key)
                .is_some_and(|m| needs_ended_recovery(m) || owes_bound_register(&self.dir, m))
        });
        for key in ended_recovery_keys(&self.mirrors) {
            recovering_ended.insert(key.clone());
            if !self.ended_pending.contains(&key) {
                self.ended_pending.push(key);
            }
        }
    }

    /// One hook datagram. Returns the key whose mirror moved, so the caller can
    /// send an early heartbeat instead of waiting out the ten-second cadence.
    fn apply_hook(&mut self, event: &lifecycle::HookEvent) -> Result<Option<String>> {
        let provider = event.provider.as_str();
        // An unknown session is not an error: a start hook wakes discovery, and the
        // 60s sweep picks up anything whose start was missed. An ended one is over.
        let Some(key) = self
            .mirrors
            .iter()
            .find(|(_, m)| {
                m.ended_at.is_none()
                    && m.native.provider == provider
                    && m.native.conversation == event.session_id
                    && match &event.source_process {
                        Some(source) => *source == lifecycle::HookProcess::of(&m.native.process),
                        // Older hook binaries can release only the exact held
                        // approval ID below. They cannot change status, end a
                        // resumed process, bind a start or acknowledge a prompt.
                        None => matches!(lifecycle::transition(event), lifecycle::Transition::Release(_)),
                    }
            })
            .map(|(key, _)| key.clone())
        else {
            return Ok(None);
        };
        // Claude writes a session's transcript only with its first turn, so one discovered
        // before that (a Claude-app session, found by its start hook) has none, and nothing
        // is uploaded until discovery's 60 s backstop looks again. Its own hook says it moved:
        // look now. One directory listing, and only while the transcript is still unknown.
        if provider == "claude" {
            let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
            if mirror.native.transcript.is_none() {
                if let Ok(Some(path)) = claude::find_transcript(&self.home, &mirror.native.conversation) {
                    mirror.native.transcript = Some(path);
                    mirror.persist(&self.dir.join(format!("{key}.json")))?;
                }
            }
        }
        match lifecycle::transition(event) {
            lifecycle::Transition::Status(status) => {
                let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
                let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                if matches!(status, "busy" | "waiting-approval") {
                    mirror.deferred_end_reason = None;
                }
                let held = mirror.waiting.as_ref().is_some_and(|prompt| prompt.held.is_some());
                // A Claude mirror's status is the engine's (`apply_engine`).
                if provider != "claude" {
                    mirror.native.status = if event.event == "Notification" && held {
                        "waiting-approval".into()
                    } else {
                        status.into()
                    };
                }
                // Claude can notify `permission_prompt` after its exact
                // PermissionRequest hook. A status-only notice cannot revoke
                // the hook that is still holding the remote approval.
                if !held || (event.event != "Notification" && status != "waiting-approval") {
                    mirror.waiting = None;
                }
                if event.last_assistant_message.is_some() {
                    mirror
                        .native
                        .last_assistant_message
                        .clone_from(&event.last_assistant_message);
                }
                persist_changed(&self.dir, &key, mirror, &before)?;
                Ok(Some(key))
            }
            lifecycle::Transition::Waiting { text } => {
                // Redacted here, not only in `event()`: the summary is also part
                // of the dedupe identity, and a mirror is persisted to disk. A
                // command line can carry a token -- redaction is a backstop, not
                // a guarantee.
                let text = text.map(|t| redact(&t));
                // A session that is not shared waits for nobody but its own terminal.
                let withheld = self.stopped_now(&key);
                // Identity is the event's own tool and reason, not the rendered
                // row: the two hooks a question fires word it differently and
                // still name one prompt.
                // A held prompt is taken only into the private hook directory, and
                // only once the hook hears back does it wait for a remote answer.
                let held = event
                    .held
                    .clone()
                    .filter(|held| {
                        !withheld
                            && enrollment::hook_path()
                                .parent()
                                .is_some_and(|dir| approval::reply_allowed(dir, &held.reply, &held.id))
                    })
                    .map(|mut held| {
                        let tool = event.tool_name.as_deref().unwrap_or("");
                        let native_send = tool == "mcp__lightbulb_native_sessions__send";
                        // The hook's private side file: the whole (bounded, redacted) input,
                        // or a native send's exact message. Never the datagram.
                        let file = approval::read_held_input(&held);
                        let private_send = file
                            .as_ref()
                            .filter(|_| native_send)
                            .and_then(|value| approval::private_send(value, &held));
                        let (input, full) = file
                            .as_ref()
                            .filter(|_| !native_send)
                            .map(|value| approval::held_input(tool, value, &held))
                            .unwrap_or((None, false));
                        held.full = full;
                        // Exact only if the reader gets the summary it was judged on: a
                        // datagram that dropped it for size shows nothing, so deny-only.
                        held.exact &= event
                            .waiting_text
                            .as_deref()
                            .is_some_and(|t| !t.trim().is_empty());
                        if event.tool_name.as_deref()
                            == Some("mcp__lightbulb_native_sessions__send")
                        {
                            held.exact &= private_send.is_some();
                        }
                        (held, private_send, input)
                    })
                    .filter(|(held, _, input)| {
                        // Do not hold a question whose complete, digest-checked input cannot
                        // be displayed. Permissions retain their separate deny-only path.
                        (event.tool_name.as_deref() != Some("AskUserQuestion")
                            || (held.full && input.as_ref().is_some_and(approval::question_relayable)))
                            && approval::acknowledge(&held.reply, &held.id)
                    });
                // A Codex hold comes from its daemon, not a hook: a hook's report of the same prompt
                // must not replace the answerable one.
                if self.mirrors.get(&key).and_then(|m| m.waiting.as_ref()?.held.as_ref()).is_some_and(daemon_held) {
                    return Ok(None);
                }
                let prompt = WaitingPrompt {
                    id: event.tool_use_id.clone(),
                    tool: event.tool_name.clone(),
                    // Trimmed like `waiting_reason` trims it, so two hooks whose
                    // padding differs still name one prompt.
                    summary: redact(event.waiting_text.as_deref().unwrap_or("").trim()),
                    held: held.as_ref().map(|(held, _, _)| held.clone()),
                    private_send: held.as_ref().and_then(|(_, private, _)| private.clone()),
                    input: held.and_then(|(_, _, input)| input),
                    shipped_full: false,
                };
                let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
                let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                mirror.deferred_end_reason = None;
                // Queued, not written through the cursor: this event is the
                // hook's, not the transcript's, and the file never carries it.
                // An open episode is not itself a reason to swallow this: a
                // question answered inside one turn can be followed by a real
                // permission prompt before any other transition clears it.
                if mirror.waiting.as_ref().is_none_or(|open| open.replace_with(&prompt)) {
                    if !withheld {
                        mirror.queue.push(approval_row(text.as_deref().unwrap_or(""), &prompt));
                    }
                    mirror.waiting = Some(prompt);
                }
                // Only the prompt a hook holds open is known here before the engine knows it.
                if provider != "claude" || mirror.waiting.as_ref().is_some_and(|prompt| prompt.held.is_some()) {
                    mirror.native.status = "waiting-approval".into();
                }
                persist_changed(&self.dir, &key, mirror, &before)?;
                Ok(Some(key))
            }
            lifecycle::Transition::Ended(reason) => {
                // This process's end, not its conversation's, and the engine ends a Claude row
                // (`apply_engine`). What only a hook knows is that its person meant it.
                if provider != "claude" {
                    self.end_session(&key, reason)?;
                } else if reason == lifecycle::ON_PURPOSE {
                    self.left_on_purpose.insert(key.clone());
                }
                Ok(Some(key))
            }
            lifecycle::Transition::Release(id) => {
                // Still waiting in the terminal, just no longer answerable from here.
                let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
                let open = mirror.waiting.clone().filter(|w| w.held.as_ref().is_some_and(|h| h.id == id));
                if let Some(mut unheld) = open {
                    let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                    unheld.held = None;
                    unheld.private_send = None;
                    unheld.input = None;
                    unheld.shipped_full = false;
                    mirror.waiting = Some(unheld.clone());
                    mirror.queue.push(approval_row("", &unheld));
                    persist_changed(&self.dir, &key, mirror, &before)?;
                    return Ok(Some(key));
                }
                Ok(None)
            }
            lifecycle::Transition::Start => {
                let mirror = self.mirrors.get(&key).ok_or("missing local session")?;
                Ok(start_links::bind(&self.dir, &mirror.native, &mirror.generation)?.then_some(key))
            }
            lifecycle::Transition::Ignore => Ok(None),
        }
    }

    /// The one end path: a `SessionEnd` hook, a pid that is gone, or the engine's word.
    fn end_session(&mut self, key: &str, reason: &str) -> Result<()> {
        let Some(mirror) = self.mirrors.get_mut(key) else {
            return Ok(());
        };
        // A withheld session never talks to the relay, so its end waits for no register or
        // upload: it ends here, also one whose end was deferred while it was still shared.
        let withheld = mirror.withheld();
        if mirror.ended_at.is_some() || (mirror.deferred_end_reason.is_some() && !withheld) {
            return Ok(());
        }
        let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
        if withheld {
            mirror.resume_generation = None;
            mirror.resume_registration_pending = false;
            // Its current generation never registered (a resume, or a new process, while
            // withheld), whichever path revived it: its end is told under the generation the
            // relay last acknowledged, if it has one, and a later share continues that chain.
            if let Some(known) = mirror.registration.acknowledged.clone().filter(|known| *known != mirror.generation) {
                mirror.generation = known;
            }
            mirror.deferred_end_reason = None;
        } else if mirror.resume_generation.is_some()
            || mirror.resume_registration_pending
            || mirror.registration.pending
        {
            mirror.resume_generation = None;
            mirror.deferred_end_reason = Some(reason.to_owned());
            // Claude's process may be gone: only the engine may call it idle.
            mirror.native.status = if mirror.native.provider == "claude" { "unknown" } else { "idle" }.into();
            persist_changed(&self.dir, key, mirror, &before)?;
            return Ok(());
        }
        // Nothing is left to promote: `advance` would put an ended mirror on another process.
        mirror.registration.successor = None;
        mirror.native.status = "ended".into();
        let ended_at_ms = epoch_ms();
        mirror.ended_at = Some(ended_at_ms / 1000);
        mirror.ended_at_ms = Some(ended_at_ms);
        mirror.ended_reason = Some(reason.into());
        persist_changed(&self.dir, key, mirror, &before)?;
        self.ended_pending.push(key.to_owned());
        Ok(())
    }

    /// A holder born before this login session ends as a person's End would: its reason is
    /// recorded first, so the provider's own exit (hook or pid) cannot claim the end as "exited";
    /// END then keeps the holder's last picture and the row offers Resume.
    fn end_stranded(&mut self, stranded: Vec<crate::holder::manager::HolderInfo>) -> Result<()> {
        for holder in stranded {
            let keys: Vec<String> = self
                .mirrors
                .iter()
                .filter(|(_, m)| {
                    m.ended_at.is_none()
                        && m.deferred_end_reason.is_none()
                        && m.native.holder.as_deref() == Some(holder.id.as_str())
                })
                .map(|(k, _)| k.clone())
                .collect();
            for key in keys {
                self.end_session(&key, super::lifecycle::LOGIN_SESSION_ENDED)?;
            }
            // END blocks up to 2 s per holder: off the host thread.
            std::thread::spawn(move || crate::holder::manager::end(&holder));
        }
        Ok(())
    }

    /// Claude Code's own rule for its background sessions (code.claude.com/docs/en/agent-view): a
    /// held session that is idle IDLE_STOP_AFTER (IDLE_STOP_PRESSED while macOS is short of
    /// memory), with no terminal or panel on its holder and nobody watching it in Lightbulb, has
    /// its provider stopped. Its conversation stays on disk; it ends as "idle-stopped" and the row
    /// offers Resume. Busy or waiting sessions, attached, watched or Only-me ones, and every
    /// session Lightbulb does not hold are never touched.
    fn stop_idle(&mut self, unattached: Vec<crate::holder::manager::HolderInfo>) -> Result<()> {
        if unattached.is_empty() || !super::session_states::reads_idle_stopped() {
            return Ok(());
        }
        let pressed = memory_pressed();
        let now = SystemTime::now();
        for holder in unattached {
            let keys: Vec<String> = self
                .mirrors
                .iter()
                .filter(|(_, m)| {
                    m.ended_at.is_none()
                        && m.deferred_end_reason.is_none()
                        && m.native.holder.as_deref() == Some(holder.id.as_str())
                })
                .map(|(k, _)| k.clone())
                .collect();
            // END stops everything the holder runs: every session in it must be due, and shared.
            // An Only-me session is never stopped here: the workspace would not be told its end.
            let due = !keys.is_empty()
                && keys.iter().all(|key| {
                    let m = &self.mirrors[key];
                    let watched = m.session_id.as_deref().is_some_and(super::screen::watched);
                    !m.withheld() && !m.stop_owed && idle_stop_due(&m.native.status, watched, quiet_for(m, now), pressed)
                });
            // The holder checks for a terminal or panel again as it takes the stop (one attached
            // since discovery looked keeps it running), and only a stop it took is recorded. It is
            // recorded before the next hook is read, so the provider's own exit cannot claim it.
            // ponytail: asked on the host thread (up to 2 s, once per due holder); a channel back from a thread if that ever shows.
            if !due || !crate::holder::manager::end_idle(&holder) {
                continue;
            }
            for key in keys {
                self.end_session(&key, super::lifecycle::IDLE_STOPPED)?;
            }
        }
        Ok(())
    }

    fn finish_deferred_end(&mut self, key: &str) -> Result<()> {
        let reason = {
            let Some(mirror) = self.mirrors.get_mut(key) else {
                return Ok(());
            };
            // A withheld session's queue is never sent, so its end does not wait for it.
            if mirror.registration.pending
                || mirror.resume_registration_pending
                || (!mirror.withheld() && (mirror.pending.is_some() || !mirror.queue.is_empty()))
                || mirror.resume_generation.is_some()
            {
                return Ok(());
            }
            mirror.deferred_end_reason.take()
        };
        if let Some(reason) = reason {
            self.end_session(key, &reason)?;
        }
        Ok(())
    }

    /// The next end the relay is still owed, with the frame that reports it.
    /// Skips what it cannot or need not report: a key whose mirror has been reaped,
    /// one a resume has revived, and one that ended before the relay ever learned
    /// the session existed -- that last one is retired here, since there is nothing
    /// to tell and nobody to tell it to.
    fn ended_frame(
        &mut self,
        recovering_ended: &mut BTreeSet<String>,
    ) -> Result<Option<(Rpc, Value)>> {
        let Self {
            dir,
            mirrors,
            ended_pending,
            relay_reads_ended_start,
            ..
        } = self;
        let mut index = ended_pending.len();
        while index > 0 {
            index -= 1;
            let key = ended_pending[index].clone();
            let Some(mirror) = mirrors.get_mut(&key) else {
                ended_pending.remove(index);
                continue;
            };
            let (Some(ended_at), false) = (mirror.ended_at, mirror.ended_sent) else {
                ended_pending.remove(index);
                continue;
            };
            if recovering_ended.contains(&key) {
                continue;
            }
            if has_drainable_backlog(mirror) {
                recovering_ended.insert(key.clone());
                continue;
            }
            // Nothing is told about a session the relay never had (no session id). One it has
            // had is told its end, also after its person stopped sharing it (`end_session`
            // keeps it on the generation the relay knows).
            let Some(session) = mirror.session_id.clone() else {
                if owes_bound_register(dir, mirror) {
                    // Someone started this run and waits to open it: the relay has to learn
                    // it existed. The draining register gives it a session, then this end.
                    recovering_ended.insert(key.clone());
                    continue;
                }
                let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                mirror.ended_sent = true;
                persist_changed(dir, &key, mirror, &before)?;
                ended_pending.remove(index);
                continue;
            };
            if codex_turn_active_after_end(mirror) || super::session_states::awaits_exit(mirror) {
                continue;
            }
            // `bound`, not `linked`: a mirror restricted since (an app restart marks it
            // `runtime-unverified` before liveness ends it) was still bound while verified.
            let start_id = start_links::bound(dir, &mirror.native, &mirror.generation);
            if start_id.is_some() && relay_reads_ended_start.is_none() {
                // Nothing has been acked on this socket: the first ack (the caller sends a
                // heartbeat for it at once) says whether this relay reads a start identity.
                continue;
            }
            // Upload and discovery run on their own cadence. Recovery keys stay
            // held until the throttled scan observes that all parent/child files
            // have drained; a plain missed end takes the direct path above.
            let reported = Rpc::Ended {
                key: key.clone(),
                at: ended_at,
                generation: mirror.generation.clone(),
                sent: Instant::now(),
            };
            let mut frame = json!({"type":"local.session.ended","sessionId":session,
                "generation":mirror.generation,
                "reason":super::session_states::end_reason(mirror),
                "endedAt":ended_at});
            // The draining register of an ended mirror carries no start identity. A relay that
            // has never heard this generation's (the whole run was offline) learns it here.
            if let Some(start_id) = start_id.filter(|_| *relay_reads_ended_start == Some(true)) {
                frame["startId"] = json!(start_id);
            }
            super::session_states::add_screen(&mut frame, mirrors, &key);
            ended_pending.remove(index);
            return Ok(Some((reported, frame)));
        }
        Ok(None)
    }

    /// A vanished approval channel is no longer remotely answerable. It may have
    /// been answered locally, crashed or expired before its release arrived; none
    /// proves the native prompt was answered. Discovery or the next lifecycle hook
    /// restores a known status. Probed with an empty datagram the hook ignores.
    fn approval_tick(&mut self) -> Vec<String> {
        let mut moved = vec![];
        for (key, mirror) in self.mirrors.iter_mut() {
            let Some(reply) = mirror
                .waiting
                .as_ref()
                .and_then(|w| w.held.as_ref())
                .map(|h| h.reply.clone())
            else {
                continue;
            };
            // A daemon hold has no hook channel: discovery follows the daemon (`codex_hold`).
            if reply == CODEX_DAEMON || mirror.ended_at.is_some() || approval::deliver(&reply, None) {
                continue;
            }
            let Ok(before) = serde_json::to_value(&*mirror) else { continue };
            let Some(mut unheld) = mirror.waiting.clone() else { continue };
            unheld.held = None;
            unheld.private_send = None;
            mirror.queue.push(approval_row("", &unheld));
            mirror.waiting = Some(unheld);
            if mirror.native.status == "waiting-approval" {
                mirror.native.status = "unknown".into();
            }
            if persist_changed(&self.dir, key, mirror, &before).is_ok() {
                moved.push(key.clone());
            }
        }
        moved
    }

    /// The engine's answer: every mirror's key on the network, and a Claude mirror's state and end.
    fn apply_engine(&mut self, answer: &Result<EngineView>) {
        let mut gone: Vec<(String, &str)> = vec![];
        for (key, m) in &mut self.mirrors {
            let before = (m.native.status.clone(), m.native.attention.clone(), m.network_id.clone(), m.deferred_end_reason.clone());
            let claude = m.native.provider == "claude" && m.ended_at.is_none();
            // A prompt a hook holds open outranks the engine until it is released (a Codex hold
            // follows its daemon: `codex_hold`).
            let held = m.waiting.as_ref().and_then(|w| w.held.as_ref()).is_some_and(|held| !daemon_held(held));
            match answer {
                Ok(view) => {
                    let same = |provider: &str, conversation: &str| provider == m.native.provider && conversation == m.native.conversation;
                    let row = view.live.iter().find(|r| same(&r.provider, &r.conversation));
                    let ended = view.ended.iter().find(|r| same(&r.provider, &r.conversation));
                    if let Some(id) = view.key(&m.native.provider, &m.native.conversation) {
                        m.network_id = Some(id.to_owned());
                    }
                    match row {
                        _ if !claude => {}
                        Some(row) => {
                            let listed = engine_view::listing_row(row);
                            m.native.status = if held { "waiting-approval" } else { claude::listing_status(&listed) }.into();
                            let note = m.native.attention.note.take();
                            m.native.attention = claude::Attention { note, ..claude::attention(&self.home, &listed, None) };
                            self.engine_missed.remove(key);
                            // It runs: an end armed on a false miss is off. One someone meant
                            // stays: the row outlives the ask for a moment.
                            if m.deferred_end_reason.as_deref().is_some_and(|reason| !super::session_states::meant_reason(reason)) {
                                m.deferred_end_reason = None;
                            }
                        }
                        // A daemon respawn leaves a gap of one poll.
                        None if self.engine_missed.insert(key.clone()) => {}
                        None if self.left_on_purpose.contains(key) => gone.push((key.clone(), lifecycle::ON_PURPOSE)),
                        None => gone.push((key.clone(), engine_view::ended_reason(ended.and_then(|e| e.reason.as_deref())))),
                    }
                }
                Err(_) if claude => {
                    m.native.status = if held { "waiting-approval" } else { "unknown" }.into();
                    // While a successor waits for `advance`, `native` is the dead predecessor.
                    if !still_running(&m.native.process) && m.registration.successor.as_ref().is_none_or(|next| !still_running(&next.process)) {
                        gone.push((key.clone(), if self.left_on_purpose.contains(key) { lifecycle::ON_PURPOSE } else { "process-exited" }));
                    }
                }
                Err(_) => {}
            }
            if before != (m.native.status.clone(), m.native.attention.clone(), m.network_id.clone(), m.deferred_end_reason.clone()) {
                self.heartbeat_dirty.insert(key.clone());
                if let Err(error) = m.persist(&self.dir.join(format!("{key}.json"))) {
                    relay_log(&format!("{} engine state not saved: {error}", short(key)));
                }
            }
        }
        for (key, reason) in gone {
            if let Err(error) = self.end_session(&key, reason) {
                relay_log(&format!("{} end not recorded: {error}", short(&key)));
            }
        }
        // ponytail: a key stays until its mirror ends, so `/exit` then a quick resume reads a later
        // crash as on purpose. Compare the hook's process with the row's if that is ever seen.
        let mirrors = &self.mirrors;
        self.left_on_purpose.retain(|key| mirrors.get(key).is_some_and(|m| m.ended_at.is_none()));
        self.engine_missed.retain(|key| mirrors.get(key).is_some_and(|m| m.ended_at.is_none()));
        let Ok(view) = answer else { return };
        let rows: Vec<_> = view.live.iter().filter(|r| r.provider == "claude").collect();
        self.engine_woken.retain(|(conversation, pid, birth)| {
            rows.iter().any(|r| r.conversation == *conversation && r.pid == *pid && r.birth_micros == *birth)
        });
        for row in rows {
            let mirrored = self.mirrors.values().any(|m| {
                let native = m.registration.successor.as_ref().unwrap_or(&m.native);
                m.ended_at.is_none()
                    && native.provider == "claude"
                    && native.conversation == row.conversation
                    && native.process.pid == row.pid
                    && native.process.birth_micros == row.birth_micros
            });
            if !mirrored && self.engine_woken.insert((row.conversation.clone(), row.pid, row.birth_micros)) {
                if let Some(wake) = &self.rediscover {
                    wake.notify_one();
                }
            }
        }
    }

    /// The poller's latest answer, if it is news and read since the last `observe`: an older one
    /// may not know a mirror that pass made.
    fn engine_tick(&mut self) {
        let Some(answer) = self
            .engine
            .as_mut()
            .filter(|engine| engine.has_changed().unwrap_or(false))
            .and_then(|engine| engine.borrow_and_update().clone())
        else {
            return;
        };
        if self.observed_at.is_none_or(|at| answer.0 >= at) {
            self.apply_engine(&answer.1);
        }
    }

    /// Every live mirror, every tick: a process that is gone ends its session. Not Claude's: the
    /// engine says (`apply_engine`).
    fn liveness_tick(&mut self) {
        let runs = |native: &NativeSession| {
            still_running(&native.process)
                // A Codex thread its shared daemon dropped is stopped though the daemon runs on.
                && (native.provider != "codex" || codex::loaded(&native.process, &native.conversation) != Some(false))
        };
        let gone: Vec<(String, &str)> = self
            .mirrors
            .iter()
            .filter(|(_, m)| {
                m.native.provider != "claude"
                    && m.ended_at.is_none()
                    && !runs(&m.native)
                    // While a successor waits for `advance`, `native` is the dead
                    // predecessor and the successor is the session that is running.
                    && m.registration.successor.as_ref().is_none_or(|next| !runs(next))
            })
            // A process still running left the conversation on purpose; only a vanished
            // process with no end event of its own is an unexpected end.
            .map(|(key, m)| (key.clone(), if still_running(&m.native.process) { "exited" } else { "process-exited" }))
            .collect();
        for (key, reason) in gone {
            if let Err(error) = self.end_session(&key, reason) {
                relay_log(&format!("{} end not recorded: {error}", short(&key)));
            }
        }
    }

    /// An ended session is kept for a day, then its mirrored history is dropped.
    /// Returns the reaped keys: a caller keeping its own per-key state (a live
    /// connection's registration map) has to drop them too, or it will index a
    /// mirror that is no longer there.
    #[must_use]
    fn reap(&mut self, protected: &BTreeSet<String>) -> Vec<String> {
        let cutoff = epoch_secs().saturating_sub(ENDED_RETENTION_SECS);
        let expired: Vec<String> = self
            .mirrors
            .iter()
            .filter(|(key, m)| {
                m.ended_at.is_some_and(|at| at < cutoff)
                    && !protected.contains(*key)
                    && !has_drainable_backlog(m)
            })
            .map(|(key, _)| key.clone())
            .collect();
        for key in &expired {
            let _ = fs::remove_file(self.dir.join(format!("{key}.json")));
            self.mirrors.remove(key);
            self.heartbeat_dirty.remove(key);
            self.ended_pending.retain(|pending| pending != key);
        }
        expired
    }
    async fn observing_wait<T>(
        &mut self,
        updates: &mut tokio::sync::mpsc::Receiver<Discovered>,
        remote: impl std::future::Future<Output = Result<T>>,
    ) -> Result<T> {
        tokio::pin!(remote);
        loop {
            tokio::select! {
                result=&mut remote=>return result,
                update=updates.recv()=>{
                    let (update, stranded, unattached, read_ms) = update.ok_or("discovery stopped")?;
                    self.discovery_read_ms = Some(read_ms);
                    self.observe(update)?;
                    self.end_stranded(stranded)?;
                    self.stop_idle(unattached)?;
                }
            }
        }
    }
    pub(super) fn restore(dir: PathBuf, home: PathBuf, stop: Arc<AtomicBool>) -> Result<Self> {
        private_dir(&dir)?;
        let mut mirrors: BTreeMap<String, Mirror> = BTreeMap::new();
        for entry in fs::read_dir(&dir).map_err(|_| "local history unavailable")? {
            let path = entry.map_err(|_| "local history unavailable")?.path();
            let Some(key) = path.file_stem().and_then(|s| s.to_str()) else {
                continue;
            };
            if path.extension().is_none_or(|s| s != "json")
                || key.len() != 64
                || !key.bytes().all(|b| b.is_ascii_hexdigit())
            {
                continue;
            }
            let mut mirror: Mirror = private_read(&path)?;
            if native_key(&mirror.native) != key {
                return Err("local history identity conflict".into());
            }
            if mirror.ended_at.is_none() {
                // Nothing about a live session is verified across a restart; the engine, a hook,
                // the pid check or the next sweep says what it really is.
                mirror.native.status = "unknown".into();
            }
            mirror.workspace.observe(
                Err("workspace not yet observed after restart".into()),
                false,
            );
            let readable = mirror.native.restriction.as_deref() == Some("history-gap")
                && readable_history(&mirror);
            if readable || mirror.native.restriction.as_deref() != Some("history-gap") {
                // A successful bounded read permits retry; discovery must still
                // reverify the native binary before control is available.
                mirror.native.restriction = Some("runtime-unverified".into());
            }
            if let Some(successor) = &mut mirror.registration.successor {
                successor.status = "unknown".into();
                if successor.restriction.as_deref() != Some("history-gap") {
                    successor.restriction = Some("runtime-unverified".into());
                }
            }
            // The downgrade above is new RAM state, not yet a durable replay.
            mirror.dirty = true;
            if mirrors.values().filter(|m| m.ended_at.is_none()).count() >= 512 {
                return Err("local session capacity reached".into());
            }
            mirrors.insert(key.into(), mirror);
        }
        let mut host = Self {
            workspace_at: Instant::now() - Duration::from_secs(30),
            workspace_key: None,
            dir,
            home,
            mirrors,
            stop,
            hooks: None,
            rediscover: None,
            ended_pending: vec![],
            relay_reads_ended_start: None,
            engine: None,
            observed_at: None,
            discovery_read_ms: None,
            engine_missed: BTreeSet::new(),
            left_on_purpose: BTreeSet::new(),
            engine_woken: BTreeSet::new(),
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
            verify: verify_native,
            // (A test's home has no shell startup files; its sessions are shared as before.)
            share_every: cfg!(test).then_some(true),
        };
        // Ended mirrors are loaded so their end can still be reported, and so the
        // day-old ones are cleaned up on exactly one predictable path. No connection
        // exists yet, so there is no per-key state to prune alongside them.
        let protected = ended_recovery_keys(&host.mirrors);
        let _ = host.reap(&protected);
        Ok(host)
    }

    async fn run_once(
        &mut self,
        enrollment: &Enrollment,
        identity: &crate::keychain::DeviceIdentity,
        wake: Arc<tokio::sync::Notify>,
        status: Arc<std::sync::Mutex<String>>,
    ) {
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        let home = self.home.clone();
        self.rediscover = Some(wake.clone());
        // Follows the shared Codex daemon's thread events (codex.rs); aborted with discovery.
        let codex_watch = tokio::spawn(codex::watch(self.home.clone(), wake.clone()));
        let (engine_tx, engine_rx) = tokio::sync::watch::channel(None);
        self.engine = Some(engine_rx);
        // Its own task, like discovery: an engine that takes its five seconds holds up nothing.
        let engine_poll = tokio::spawn(poll_engine(engine_tx, engine_view::read));
        let discovery = async move {
            // A 60s backstop, not the status source: the engine (Claude), hooks and the pid
            // check drive status now. A wake (a start hook, an enrollment change) runs it sooner,
            // but never more than once a second.
            let mut last = Instant::now() - Duration::from_secs(1);
            let mut last_failures: Vec<String> = vec![];
            loop {
                if let Some(remaining) = Duration::from_secs(1).checked_sub(last.elapsed()) {
                    tokio::time::sleep(remaining).await;
                }
                last = Instant::now();
                let read_ms = epoch_ms();
                let (claude, codex) = tokio::join!(claude::discover(&home), codex::discover(&home));
                // Cursor and Grok CLI: whatever their hooks recorded (agent network phase 2).
                let hooked: Result<Vec<NativeSession>> = Ok(super::hooked::discover(&super::hooked::dir()));
                // One provider's broken listing must not hide the other's sessions,
                // so the pass is the union of whatever answered.
                // Attach reaping reads the raw Claude listing, not the sessions that captured: a
                // row whose capture failed is still running. A failed listing says nothing (None).
                let (claude, running) = match claude {
                    Ok(d) => (Ok(d.sessions), Some(d.jobs)),
                    Err(e) => (Err(e), None),
                };
                let claude_ok = running.is_some();
                let listings = [claude, codex, hooked];
                let providers = listings.len();
                let mut found = vec![];
                let mut failures = vec![];
                for listing in listings {
                    match listing {
                        Ok(sessions) => found.extend(sessions),
                        Err(error) => failures.push(error),
                    }
                }
                // Every session is tracked; this names the held ones (rulings 2026-10-04).
                // One process table for both: the list and which of it is stranded.
                let (holders, stranded, unattached) = tokio::task::spawn_blocking(|| {
                    let holders = crate::holder::manager::list();
                    let stranded = crate::holder::manager::stranded(&holders);
                    let unattached = crate::holder::manager::unattached(&holders);
                    (holders, stranded, unattached)
                })
                .await
                .unwrap_or_default();
                // All holders, stranded ones too: `native.holder` stays set until they end.
                track(&mut found, &holders);
                let running = running.unwrap_or_default();
                super::screen::reap_attach(&holders, claude_ok.then_some(&running));
                for error in &failures {
                    // A listing that failed is not evidence that anything stopped;
                    // say so once per distinct failure, not once a minute forever.
                    if !last_failures.contains(error) {
                        relay_log(&format!("discovery unavailable: {error}"));
                    }
                }
                // Every provider failed: this pass proves nothing. Keep the state we
                // have -- an empty listing would otherwise read as "all quiet".
                if failures.len() < providers && tx.send((found, stranded, unattached, read_ms)).await.is_err() {
                    break;
                }
                last_failures = failures;
                tokio::select! {_ = tokio::time::sleep(Duration::from_secs(60))=>{},_ = wake.notified()=>{}}
            }
        };
        let network = async {
            let mut delay = 1;
            loop {
                if self.stop.load(Ordering::SeqCst) {
                    break;
                }
                let mut lived = Duration::ZERO;
                let result: Result<()> = async {
                    let token = crate::device_token::mint_device_token(
                        &identity.private_key_pem,
                        &enrollment.device_id,
                        &enrollment.owner_id,
                        90,
                    )?;
                    let mut url = url::Url::parse(&enrollment.gateway_url)
                        .map_err(|_| "invalid paired gateway")?;
                    if url.scheme() != "wss" {
                        return Err("invalid paired gateway".into());
                    }
                    url.query_pairs_mut()
                        .clear()
                        .append_pair("clientId", "lightbulb-local-sessions")
                        .append_pair("token", &token);
                    let config =
                        tokio_tungstenite::tungstenite::protocol::WebSocketConfig::default()
                            .max_message_size(Some(16384))
                            .max_frame_size(Some(16384));
                    let (mut socket, _) = self
                        .observing_wait(&mut rx, async {
                            tokio::time::timeout(
                                Duration::from_secs(10),
                                tokio_tungstenite::connect_async_with_config(
                                    url.as_str(),
                                    Some(config),
                                    false,
                                ),
                            )
                            .await
                            .map_err(|_| "relay unavailable")?
                            .map_err(|_| "relay unavailable or pairing revoked".to_string())
                        })
                        .await?;
                    if let Ok(mut s) = status.lock() {
                        *s = "connected".into();
                    }
                    relay_log(&format!(
                        "connected; {} mirrored sessions",
                        self.mirrors.len()
                    ));
                    let started = Instant::now();
                    let outcome = self
                        .connection(&mut socket, &mut rx, &enrollment.human_user_id)
                        .await;
                    lived = started.elapsed();
                    outcome
                }
                .await;
                relay_log(&format!(
                    "connection ended after {:.1}s: {}",
                    lived.as_secs_f64(),
                    match &result {
                        Ok(()) => "paused".to_owned(),
                        Err(e) => e.to_string(),
                    }
                ));
                if let Ok(mut s) = status.lock() {
                    *s = if result.is_err() {
                        "disconnected"
                    } else {
                        "paused"
                    }
                    .into();
                }
                if let Err(e) = self
                    .observing_wait(&mut rx, async {
                        tokio::time::sleep(Duration::from_secs(delay)).await;
                        Ok(())
                    })
                    .await
                {
                    relay_log(&format!("network loop stopped: {e}"));
                    break;
                }
                delay = reconnect_delay(delay, lived);
            }
        };
        // Its own task: a slow pass (transcript reads, lsof) must never hold up the
        // connection loop that reads the relay's answers.
        let mut discovery = tokio::spawn(discovery);
        tokio::select! {_ = &mut discovery=>{},_ = network=>{}}
        discovery.abort();
        codex_watch.abort();
        engine_poll.abort();
    }

    pub(super) async fn supervise(
        mut self,
        enrollment: Enrollment,
        identity: crate::keychain::DeviceIdentity,
        wake: Arc<tokio::sync::Notify>,
        status: Arc<std::sync::Mutex<String>>,
        hooks: tokio::sync::mpsc::Receiver<super::lifecycle::HookEvent>,
    ) {
        self.hooks = Some(hooks);
        let mut restart = 1u64;
        loop {
            // Release builds abort on panic (Cargo.toml), so a panic is a visible crash, never a
            // silent dark relay; this loop covers the loops ending on their own.
            let started = Instant::now();
            self.run_once(&enrollment, &identity, wake.clone(), status.clone())
                .await;
            // Measured here, before the status write and the backoff sleep: a sustained outage's
            // own sleep must never count toward "lived a healthy while" and reset the cap early.
            let lived = started.elapsed();
            if let Ok(mut s) = status.lock() {
                *s = "disconnected".into();
            }
            if self.stop.load(Ordering::SeqCst) {
                return;
            }
            relay_log(&format!("supervisor loop ended; restarting in {restart}s"));
            tokio::time::sleep(Duration::from_secs(restart)).await;
            restart = restart_delay(restart, lived);
        }
    }

    async fn connection(
        &mut self,
        socket: &mut Socket,
        updates: &mut tokio::sync::mpsc::Receiver<Discovered>,
        human: &str,
    ) -> Result<()> {
        // Feature negotiation is scoped to this socket. A reconnect must start
        // unknown so it can recover child journals before the new ACK arrives.
        self.reset_relay_features();
        let mut registered = BTreeSet::<String>::new();
        let mut quarantined = BTreeMap::<String, Instant>::new();
        let mut pending_rpc: Option<Rpc> = None;
        let mut anchor: Option<ClockAnchor> = None;
        let mut deferred = std::collections::VecDeque::<Command>::new();
        let mut sent_batches = BTreeMap::<String, Instant>::new();
        // When each session was last offered an upload, or last had one acked. Per
        // session: the relay's limit is two text batches a second for ONE session, and a
        // shared slot let one busy session hold every other session's history back.
        let mut last_upload = BTreeMap::<String, Instant>::new();
        let mut heartbeat_frames = std::collections::VecDeque::<Value>::new();
        let mut last_heartbeat = Instant::now();
        let mut last_idle = Instant::now();
        let mut last_early = Instant::now() - Duration::from_secs(1);
        let mut tick = tokio::time::interval(Duration::from_millis(100));
        // This connection carries the live terminal frames of every watched held session.
        let mut screens = super::screen::channel();
        let (asks, asked) = tokio::sync::mpsc::unbounded_channel();
        let (answers, mut reconciled) = tokio::sync::mpsc::unbounded_channel();
        tokio::spawn(reconcile_worker(self.dir.clone(), self.home.clone(), self.verify, asked, answers));
        let mut reconciles = Reconciles { asks, state: BTreeMap::new() };
        let mut unknown_frame_logged = false;
        let mut reaped = Instant::now();
        let mut last_recovery_scan = Instant::now();
        let mut last_probe = Instant::now();
        // Learned from the relay's own registration ACK; never assumed.
        let mut relay_reads_stats = false;
        let mut relay_reads_controls = false;
        let mut relay_reads_private_approval = false;
        let mut relay_reads_approval_input = false;
        let mut relay_reads_live_terminal = false;
        let mut relay_reads_remote_input = false;
        let mut relay_reads_terminal = false;
        let mut relay_reads_presence = false;
        let mut relay_reads_pickups = false;
        let mut relay_reads_band_owner_only = false;
        let mut relay_reads_agent_asks = false;
        // An end the relay has not been told about -- because it happened while the
        // socket was down, or in a previous run of this process -- is still owed to it.
        self.liveness_tick();
        self.engine_tick();
        super::session_states::index(&self.mirrors);
        let mut recovering_ended = ended_recovery_keys(&self.mirrors);
        let mut unreported: Vec<String> = self
            .mirrors
            .iter()
            .filter(|(key, m)| {
                m.ended_at.is_some() && !m.ended_sent && !self.ended_pending.contains(*key)
            })
            .map(|(key, _)| key.clone())
            .collect();
        for key in &recovering_ended {
            if !self.ended_pending.contains(key) && !unreported.contains(key) {
                unreported.push(key.clone());
            }
        }
        self.ended_pending.extend(unreported);
        let mut laps = Laps::new();
        let mut arm = "start";
        loop {
            laps.next(arm);
            if self.stop.load(Ordering::SeqCst) {
                return Ok(());
            }
            // Before anything this turn sends: what this Mac's person asked since the last one.
            for key in self.share_tick() {
                registered.remove(&key);
            }
            // One bounded checkout scan per 30s across the entire host, not per session/tick.
            if self.workspace_at.elapsed() >= Duration::from_secs(30) {
                self.workspace_at = Instant::now();
                let keys: Vec<_> = self
                    .mirrors
                    .iter()
                    .filter(|(_, m)| m.ended_at.is_none())
                    .map(|(k, _)| k.clone())
                    .collect();
                if let Some(key) = keys
                    .iter()
                    .find(|k| self.workspace_key.as_ref().is_none_or(|last| *k > last))
                    .or(keys.first())
                {
                    self.observe_workspace(key).await?;
                    self.workspace_key = Some(key.clone());
                }
            }
            // Git, gh and subagent counts for the rows: started here, read off the loop.
            super::signals::tick(&self.mirrors, &self.home);
            laps.lap("workspace scan");
            if anchor.as_ref().is_some_and(|a| a.now().is_none()) {
                anchor = None;
            }
            if anchor.is_some() {
                let ready = deferred.iter().position(|cmd| {
                    // A withheld session's command is answered too: `command` rejects it.
                    self.mirrors.iter().any(|(key, m)| {
                        (registered.contains(key) || m.withheld()) && m.session_id.as_deref() == Some(&cmd.session_id)
                    })
                });
                if let Some(command) = ready.and_then(|index| deferred.remove(index)) {
                    let receipt = self.command(&command, anchor.as_ref()).await?;
                    send(
                        socket,
                        &receipt_wire(&command.session_id, &command.request_id, &receipt),
                    )
                    .await?;
                }
            }
            laps.lap("deferred command");
            if pending_rpc.is_none() {
                if let Some(key) = registration_due_with_recovery(
                    &self.mirrors,
                    &registered,
                    &quarantined,
                    &recovering_ended,
                ) {
                    let sent = Instant::now();
                    let wall = SystemTime::now();
                    let begun = if recovering_ended.contains(&key) {
                        self.begin_ended_recovery(&key)
                    } else {
                        self.begin_registration(&key)
                    };
                    // Not saved (a full disk): this session waits, the connection and every
                    // other session stay.
                    let body = match begun {
                        Ok(body) => body,
                        Err(error) => {
                            quarantine(&mut quarantined, &mut registered, &mut sent_batches, &key, &error);
                            continue;
                        }
                    };
                    // Ended recovery overrides the wire reason without changing the
                    // native verdict; only a changed native verdict needs another register.
                    let native_restriction = self.mirrors[&key].native.restriction.clone();
                    send(socket, &body).await?;
                    let start_id = body["session"]["startId"].as_str().map(str::to_owned);
                    pending_rpc = Some(Rpc::Register { key, sent, wall, native_restriction, start_id });
                } else {
                    // One heartbeat for the device, not one per session: a session whose
                    // fields moved brings the whole cadence forward instead of sending
                    // its own frame, and no more than once a second.
                    let early = !self.heartbeat_dirty.is_empty()
                        && last_early.elapsed() >= Duration::from_secs(1);
                    if heartbeat_frames.is_empty()
                        && (early || last_heartbeat.elapsed() >= self.heartbeat_interval)
                    {
                        last_heartbeat = Instant::now();
                        if early {
                            last_early = Instant::now();
                        }
                        self.heartbeat_dirty.clear();
                        let frames = device_heartbeats_with_controls(&self.mirrors, &registered, relay_reads_stats, relay_reads_controls, relay_reads_live_terminal, relay_reads_remote_input, relay_reads_terminal, relay_reads_presence, relay_reads_pickups, relay_reads_band_owner_only, relay_reads_agent_asks)?;
                        let frames = super::session_states::with_screens(frames, &self.mirrors);
                        super::presence::retain(&frames.iter().flat_map(super::presence::frame_sids).collect::<Vec<_>>());
                        heartbeat_frames = frames.into();
                        if heartbeat_frames.is_empty() && last_idle.elapsed() >= IDLE_HEARTBEAT {
                            last_idle = Instant::now();
                            heartbeat_frames.push_back(idle_heartbeat());
                        }
                    }
                    if let Some((reported, frame)) = self.ended_frame(&mut recovering_ended)? {
                        send(socket, &frame).await?;
                        pending_rpc = Some(reported);
                    } else if let Some(frame) = heartbeat_frames.pop_front().or_else(|| {
                        // An end is waiting to learn what this relay reads, and nothing
                        // registers here: ask now, not at the idle heartbeat 30 s on.
                        (self.relay_reads_ended_start.is_none()
                            && self.ended_pending.iter().any(|key| {
                                // A recovering key registers on this socket: that ack says it.
                                !recovering_ended.contains(key) && self.mirrors.get(key).is_some_and(|m| {
                                    start_links::bound(&self.dir, &m.native, &m.generation).is_some()
                                })
                            }))
                        .then(idle_heartbeat)
                    }) {
                        let sent = Instant::now();
                        let wall = SystemTime::now();
                        send(socket, &frame).await?;
                        pending_rpc = Some(Rpc::Heartbeat { sent, wall, sids: super::presence::frame_sids(&frame) });
                    }
                }
            }
            laps.lap("registration or heartbeat");
            {
                let keys: Vec<_> = self.mirrors.keys().cloned().collect();
                for key in &keys {
                    if !registered.contains(key)
                        || last_upload
                            .get(key)
                            .is_some_and(|at| at.elapsed() < Duration::from_millis(500))
                        || sent_batches
                            .get(key)
                            .is_some_and(|at| at.elapsed() < Duration::from_secs(2))
                    {
                        continue;
                    }
                    if self.stopped_now(key) {
                        continue;
                    }
                    last_upload.insert(key.clone(), Instant::now());
                    let mirror = self.mirrors.get_mut(key).ok_or("missing local session")?;
                    let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                    let batch = match capture_batch(mirror) {
                        Ok(batch) => batch,
                        Err(reason) => {
                            relay_log(&format!("{} history unreadable: {reason}", short(key)));
                            registered.remove(key);
                            persist_changed(&self.dir, key, mirror, &before)?;
                            continue;
                        }
                    };
                    let batch = batch.map(|events| {
                        let events = if relay_reads_private_approval {
                            attach_private_send(mirror, events)
                        } else {
                            events
                        };
                        if relay_reads_approval_input {
                            attach_approval_input(mirror, events)
                        } else {
                            events
                        }
                    });
                    persist_changed(&self.dir, key, mirror, &before)?;
                    if let Some(batch) = batch {
                        send(socket, &wire_batch(batch)).await?;
                        sent_batches.insert(key.clone(), Instant::now());
                    }
                }
            }
            laps.lap("uploads");
            let deadline = pending_rpc.as_ref().map(|rpc| rpc.sent() + ACK_DEADLINE);
            tokio::select! {
                _ = tick.tick() => {
                    arm = laps.woke("tick");
                    self.liveness_tick();
                    self.engine_tick();
                    if last_recovery_scan.elapsed() >= Duration::from_secs(1) {
                        last_recovery_scan = Instant::now();
                        self.refresh_ended_recovery(&mut recovering_ended);
                    }
                    // (A session that just ended in a holder is indexed now: its viewers wait for the kept picture.)
                    let ended_now = super::session_states::ended_now();
                    let probe = last_probe.elapsed() >= Duration::from_secs(1);
                    if probe {
                        last_probe = Instant::now();
                        let moved = self.approval_tick();
                        self.heartbeat_dirty.extend(moved);
                    }
                    if probe || ended_now {
                        let kept = super::session_states::index(&self.mirrors);
                        let newly = !kept.is_empty();
                        match super::session_states::screens_now(&self.mirrors).filter(|_| newly) {
                            // Ahead of whatever heartbeat frames are still to go.
                            Some(screens) => heartbeat_frames.push_front(screens),
                            None => self.heartbeat_dirty.extend(kept),
                        }
                    }
                    if reaped.elapsed() >= Duration::from_secs(60) {
                        reaped = Instant::now();
                        // This connection's own per-key state has to go with them.
                        for key in self.reap(&recovering_ended) {
                            registered.remove(&key);
                            sent_batches.remove(&key);
                            last_upload.remove(&key);
                            quarantined.remove(&key);
                        }
                    }
                },
                event = async {
                    match self.hooks.as_mut() {
                        Some(hooks) => hooks.recv().await,
                        // No hook listener on this host: the arm never fires.
                        None => std::future::pending().await,
                    }
                } => {
                    arm = laps.woke("hook");
                    match event {
                        Some(event) => {
                            // A turn can start and finish before discovery first sees its
                            // session (a Codex rollout appears only with the first prompt), so
                            // its events would land on nothing until the 60s sweep. Look now.
                            if !self.mirrors.values().any(|m| m.ended_at.is_none() && m.native.conversation == event.session_id) {
                                if let Some(wake) = &self.rediscover { wake.notify_one(); }
                            }
                            if let Some(key) = self.apply_hook(&event)? {
                                if event.event == "SessionStart" { registered.remove(&key); }
                                else { self.heartbeat_dirty.insert(key); }
                            }
                        },
                        // The listener is gone; stop polling a closed channel.
                        None => self.hooks = None,
                    }
                },
                update = updates.recv() => {
                    arm = laps.woke("discovery update");
                    let Some((update, stranded, unattached, read_ms)) = update else {return Err("discovery stopped".into());};
                    self.discovery_read_ms = Some(read_ms);
                    for key in self.observe(update)? {
                        registered.remove(&key);
                        recovering_ended.remove(&key);
                    }
                    self.end_stranded(stranded)?;
                    self.stop_idle(unattached)?;
                },
                Some(frame) = screens.recv() => {
                    arm = laps.woke("screen frame");
                    // Fire and forget: the relay never answers a screen frame. The one place every
                    // terminal frame passes: one queued before its session was withheld is dropped.
                    if !self.refuses_frame(&frame) {
                        send(socket, &frame).await?;
                    }
                },
                Some((ask, answer)) = reconciled.recv() => {
                    arm = laps.woke("reconciliation receipt");
                    let receipt = answer?;
                    reconciles.answered(&ask, receipt.outcome == "unknown");
                    self.acknowledge_workspace(&ask.session, &ask.generation, &receipt)?;
                    send(socket, &receipt_wire(&ask.session, &ask.request, &receipt)).await?;
                },
                incoming = next_or_deadline(socket, deadline) => {
                    arm = laps.woke("relay frame");
                    let message = incoming?.ok_or("session socket closed")?.map_err(|_| "session socket failed")?;
                    let Message::Text(text) = message else {
                        if matches!(message, Message::Close(_)) {return Err("session socket closed".into());}
                        if let Message::Ping(bytes) = message {socket.send(Message::Pong(bytes)).await.map_err(|_| "session socket failed")?;}
                        continue;
                    };
                    if text.len() > 16384 {return Err("oversized session frame".into());}
                    let body: Value = serde_json::from_str(&text).map_err(|_| "invalid session frame")?;
                    match body["type"].as_str() {
                        Some("local.session.registered") => {
                            let Some(rpc) = pending_rpc.take() else { late_ack(); continue; };
                            let Rpc::Register{key,sent,wall,native_restriction,start_id} = rpc else {return Err("mismatched session acknowledgment".into());};
                            if quarantine_active(&quarantined, &key) {
                                // A late ACK must never put a quarantined session back in rotation.
                                relay_log(&format!("{} ack ignored while quarantined", short(&key)));
                                continue;
                            }
                            relay_reads_stats = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "sessionStats"));
                            relay_reads_controls = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "nativeControlCapabilities"));
                            relay_reads_private_approval = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "privateSendApproval"));
                            relay_reads_approval_input = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "approvalInput"));
                            relay_reads_live_terminal = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "liveTerminal"));
                            relay_reads_remote_input = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "remoteInput"));
                            relay_reads_terminal = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "sessionTerminal"));
                            super::session_states::read_features(&body["features"]);
                            relay_reads_presence = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "sessionPresence"));
                            relay_reads_pickups = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "pickupRequests"));
                            relay_reads_band_owner_only = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "bandOwnerOnly"));
                            relay_reads_agent_asks = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "agentMessageAsks"));
                            self.relay_reads_ended_start = Some(reads_ended_start(&body));
                            let sid = body["sessionId"].as_str().filter(|s|uuid::Uuid::parse_str(s).is_ok()).ok_or("invalid session ID")?;
                            let after = body["afterSeq"].as_u64().ok_or("missing upload cursor")?;
                            let server = body["serverTime"].as_u64().ok_or("missing server time")?;
                            let mirror = self.mirrors.get(&key).ok_or("unknown local session")?;
                            let verdict = if body["generation"] != mirror.generation {
                                Some("stale generation acknowledgment")
                            } else if mirror.session_id.as_ref().is_some_and(|known| known != sid) {
                                Some("session identity changed")
                            } else {
                                None
                            };
                            if let Some(reason) = verdict {
                                quarantine(&mut quarantined, &mut registered, &mut sent_batches, &key, reason);
                                continue;
                            }
                            let mirror = self.mirrors.get_mut(&key).ok_or("unknown local session")?;
                            let before=serde_json::to_value(&*mirror).map_err(|e|e.to_string())?;
                            let pending_id = mirror.pending.as_ref().and_then(|p| p["batchId"].as_str()).map(str::to_owned);
                            let pending_count = mirror.pending.as_ref().and_then(|p| p["events"].as_array()).map(|e| e.len() as u64).unwrap_or(0);
                            if after == mirror.after_seq {
                                // Nothing was applied since the last ACK.
                            } else if let (Some(id), true) = (pending_id.as_deref(), pending_count > 0 && after == mirror.after_seq + pending_count) {
                                // The relay applied the in-flight batch; its ACK never reached this Mac.
                                mirror.acknowledge(id, after)?;
                                relay_log(&format!("{} reconciled a lost ack: cursor {after}", short(&key)));
                            } else {
                                relay_log(&format!("{} cursor resync: local {} (+{pending_count} pending) relay {after}", short(&key), mirror.after_seq));
                                mirror.resync(after);
                            }
                            if !relay_reads_private_approval && mirror.pending.as_ref().is_some_and(|batch| batch.get("privateApproval").is_some()) {
                                mirror.pending = None;
                            }
                            if !relay_reads_approval_input && mirror.pending.as_ref().is_some_and(carries_approval_input) {
                                mirror.pending = None;
                                if let Some(waiting) = mirror.waiting.as_mut() {
                                    waiting.shipped_full = false;
                                }
                            }
                            let first_id = mirror.session_id.is_none();
                            mirror.session_id = Some(sid.into());
                            mirror.subagents.relay =body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "sessionSubagents"));
                            mirror.subagents.relay_known = true;
                            mirror.registered();
                            relay_log(&format!("{} registered as {} at cursor {after}", short(&key), short(sid)));
                            let advanced = mirror.advance();
                            persist_changed(&self.dir,&key,mirror,&before)?;
                            // Whatever discovery has found since, the relay has this
                            // session: fields it has not seen ride the next heartbeat,
                            // and only the relay's own rejection re-registers it.
                            // Unless `advance` just promoted a successor -- the relay
                            // has never seen THAT generation, and uploading under it
                            // would only earn a rejection.
                            // An ended mirror's draining register carries no start identity.
                            let current_start_id = start_links::linked(&self.dir, &mirror.native, &mirror.generation)
                                .filter(|_| mirror.ended_at.is_none());
                            if advanced
                                || native_restriction != mirror.native.restriction
                                || start_id != current_start_id
                                || mirror.registration.sent_withheld != mirror.withheld()
                            {
                                registered.remove(&key);
                            } else {
                                registered.insert(key.clone());
                            }
                            if first_id {
                                // A fork registered before this id existed went out without
                                // `forkedFrom`; register it once more so the relay links it.
                                for fork in forks_of(&self.mirrors, &key) {
                                    registered.remove(&fork);
                                }
                            }
                            self.finish_deferred_end(&key)?;
                            // A register that had nothing to drain is done: its end is next.
                            self.refresh_ended_recovery(&mut recovering_ended);
                            // Someone watches it now: stream at once, not after every other
                            // session has registered and the first heartbeat is acked.
                            if body["watch"].is_object() {super::screen::nudge(&body["watch"]);}
                            if let Some(fresh) = ack_anchor(server, sent, wall) {anchor = Some(fresh);}
                            if !body["reconcile"].is_null() {
                                let request = body["reconcile"]["requestId"].as_str().ok_or("invalid reconciliation")?;
                                let generation = body["reconcile"]["generation"].as_str().ok_or("invalid reconciliation")?;
                                self.dispatch_reconcile(socket,&mut deferred,anchor.as_ref(),&mut reconciles,sid,request,generation,
                                    body["reconcile"]["expectedTurnId"].as_str()).await?;
                            }
                        },
                        Some("local.device.heartbeat.ack") => {
                            let Some(rpc) = pending_rpc.take() else { late_ack(); continue; };
                            let Rpc::Heartbeat{sent,wall,sids} = rpc else {return Err("mismatched session acknowledgment".into());};
                            super::snapshot::offer(&body["pickup"], &self.mirrors);
                            super::screen::offer(&body["watching"]);
                            super::session_states::read_ack(&body);
                            self.relay_reads_ended_start = Some(reads_ended_start(&body));
                            super::presence::offer_ack(&body, &sids);
                            for entry in body["presence"].as_array().into_iter().flatten().take(64) {
                                if let Some(key) = self.follow_presence(entry) { registered.remove(&key); }
                            }
                            super::resume::offer(&body["resume"], &self.mirrors);
                            let server = body["serverTime"].as_u64().ok_or("missing server time")?;
                            if let Some(fresh) = ack_anchor(server, sent, wall) {anchor = Some(fresh);}
                            // A session the relay does not hold is one this Mac has to
                            // register again; it is never heartbeated back into existence.
                            for rejected in body["rejected"].as_array().into_iter().flatten() {
                                if let Some(key) = session_key(&self.mirrors, rejected.as_str()) {
                                    relay_log(&format!("{} rejected by the relay; re-registering", short(&key)));
                                    registered.remove(&key);
                                }
                            }
                            for entry in body["reconcile"].as_array().into_iter().flatten() {
                                let Some(sid) = entry["sessionId"].as_str().filter(|sid| session_key(&self.mirrors, Some(sid)).is_some()) else {
                                    relay_log("reconciliation for a session this Mac does not hold, ignored");
                                    continue;
                                };
                                let request = entry["requestId"].as_str().ok_or("invalid reconciliation")?;
                                let generation = entry["generation"].as_str().ok_or("invalid reconciliation")?;
                                self.dispatch_reconcile(socket,&mut deferred,anchor.as_ref(),&mut reconciles,sid,request,generation,
                                    entry["expectedTurnId"].as_str()).await?;
                            }
                        },
                        Some("local.session.ended.ack") => {
                            let Some(rpc) = pending_rpc.take() else { late_ack(); continue; };
                            let Rpc::Ended{key,at,generation,..} = rpc else {return Err("mismatched session acknowledgment".into());};
                            super::snapshot::offer(&body["pickup"], &self.mirrors);
                            // Told. Nothing more is uploaded, heartbeated or registered for
                            // it; the mirror stays until it is reaped.
                            registered.remove(&key);
                            sent_batches.remove(&key);
                            let Some(mirror) = self.mirrors.get_mut(&key) else {continue;};
                            // This ACK answers one specific end. A resume -- and a second end
                            // -- can both land while it is in flight, and the session running
                            // now still owes its own end, so anything but the exact end this
                            // frame reported leaves `ended_sent` alone for `ended_frame`.
                            if mirror.ended_at != Some(at) || mirror.generation != generation {continue;}
                            recovering_ended.remove(&key);
                            let before=serde_json::to_value(&*mirror).map_err(|e|e.to_string())?;
                            mirror.ended_sent = true;
                            if let Some(session) = &mirror.session_id {
                                crate::holder::manager::end_asked_forget(session);
                            }
                            persist_changed(&self.dir,&key,mirror,&before)?;
                        },
                        Some("local.session.events.ack") => {
                            let finish_key = {
                                let (key,mirror) = self.mirrors.iter_mut().find(|(_,m)|m.session_id.as_deref() == body["sessionId"].as_str()).ok_or("unknown upload acknowledgment")?;
                                let batch_id = body["batchId"].as_str().ok_or("missing batch ID")?;
                                let after = body["afterSeq"].as_u64().ok_or("missing cursor")?;
                                // `paused`: sharing is paused in the workspace and the relay stored
                                // none of this batch. The pause is this Mac's too from here.
                                let paused = body["paused"] == true;
                                let taken = if paused { mirror.dropped_paused(batch_id, after)? } else { mirror.acknowledge(batch_id, after)? };
                                if taken {
                                    if paused {
                                        relay_log(&format!("{} paused in the workspace; its upload waits", short(key)));
                                        super::screen::revoke(body["sessionId"].as_str().unwrap_or_default());
                                    }
                                    if mirror.advance() {registered.remove(key);}
                                    mirror.persist(&self.dir.join(format!("{key}.json")))?;
                                    sent_batches.remove(key);
                                    // Spaced from the ack, not the send: the relay starts its
                                    // half second when it stores the batch.
                                    last_upload.insert(key.clone(), Instant::now());
                                } else {
                                    relay_log(&format!("{} ignored a stale upload ack at {after}", short(key)));
                                }
                                key.clone()
                            };
                            self.finish_deferred_end(&finish_key)?;
                        },
                        Some("local.session.error") => {
                            let code = body["code"].as_str().unwrap_or("?").to_owned();
                            if code == "INTERNAL" {
                                // The relay failed, not this frame. It answers only the in-flight
                                // request (register, heartbeat or end): free the slot so that frame
                                // goes again on its own cadence. Never a quarantine.
                                relay_log("relay failed a request on its side; retrying");
                                pending_rpc = None;
                                continue;
                            }
                            let named = body["sessionId"].as_str().and_then(|sid| {
                                self.mirrors.iter().find(|(_, m)| m.session_id.as_deref() == Some(sid)).map(|(k, _)| k.clone())
                            });
                            let inflight = pending_rpc.as_ref().and_then(Rpc::key).map(str::to_owned);
                            let attributed = named.is_some();
                            let Some(key) = named.or(inflight) else {
                                relay_log(&format!("relay rejected an unattributable frame: {code}"));
                                return Err("local session relay rejected the frame".into());
                            };
                            if code == "RATE_LIMITED" {
                                // Retryable by contract: the pending batch re-sends on the 2s
                                // sent_batches throttle. Quarantining it would go dark for 5min.
                                relay_log(&format!("{} rate limited; pending batch retries after the throttle", short(&key)));
                                continue;
                            }
                            // A named error is about this session's events frame only while a
                            // batch is outstanding; in every other case it is the in-flight RPC's
                            // own reply, and holding the slot would stall until the ack deadline.
                            if pending_rpc.as_ref().and_then(Rpc::key) == Some(key.as_str())
                                && !(attributed && sent_batches.contains_key(&key))
                            {
                                pending_rpc = None;
                            }
                            quarantine(&mut quarantined, &mut registered, &mut sent_batches, &key, &format!("relay rejected a frame: {code}"));
                        },
                        Some("local.session.command") => {
                            let command = parse_command(&body, human)?;
                            if deferred.len() >= 32 {return Err("too many deferred commands".into());}
                            if anchor.is_none() && pending_rpc.is_none() {return Err("command without authenticated time".into());}
                            deferred.push_back(command);
                        },
                        Some("local.session.receipt.ack") => {},
                        // Someone just started watching: stream now. Never answered.
                        Some("local.session.watch") => if !self.refuses_frame(&body) { super::screen::nudge(&body) },
                        // A teammate's keystrokes: queued for the holder, never answered.
                        Some("local.session.input") => if !self.refuses_frame(&body) { super::remote_input::offer(&body) },
                        // Who watches, who may type, join requests, pause. Never answered.
                        Some("local.session.presence") => {
                            super::presence::offer_frame(&body);
                            if let Some(key) = self.follow_presence(&body) { registered.remove(&key); }
                        },
                        other => {
                            // Not a frame of this protocol: API Gateway's own `{"message":"Internal
                            // server error"}` when a backend call outlived its Lambda (10 s), or a
                            // newer relay's. Dropping the connection for it took every session offline.
                            if !unknown_frame_logged {
                                unknown_frame_logged = true;
                                let kind: String = other.unwrap_or("no type").chars().take(48).collect();
                                relay_log(&format!("ignored a frame this app does not know ({kind}); later ones on this connection are not logged"));
                            }
                            // The in-flight request keeps its slot: the Lambda gave up, not the
                            // backend, and the ack still comes. Freed here, the next request took
                            // that late ack as its own and its own ack then answered nothing.
                        },
                    }
                }
            }
        }
    }

    async fn verified(&self, native: &NativeSession) -> Result<NativeSession> {
        (self.verify)(self.home.clone(), native.clone()).await
    }

    /// Answer one reconciliation the relay attached to an acknowledgment. Dispatch
    /// precedes the ACK on the real relay, so the matching bounded pre-ACK frame is
    /// usually already buffered; admit that before considering a missing-journal
    /// tombstone.
    async fn dispatch_reconcile(
        &mut self,
        socket: &mut Socket,
        deferred: &mut std::collections::VecDeque<Command>,
        anchor: Option<&ClockAnchor>,
        reconciles: &mut Reconciles,
        session: &str,
        request: &str,
        generation: &str,
        expected_turn: Option<&str>,
    ) -> Result<()> {
        let buffered = deferred
            .iter()
            .position(|cmd| cmd.session_id == session && cmd.request_id == request);
        let Some(cmd) = buffered.and_then(|index| deferred.remove(index)) else {
            // The journal read stays here, in order with admission (its tombstone fences a late
            // command frame). Whatever it cannot answer is checked off this loop.
            let journal = Journal { dir: self.dir.join("receipts") };
            private_dir(&journal.dir)?;
            let receipt = journal.reconcile(session, request, generation)?;
            let turn = expected_turn.filter(|_| !receipt.settled).and_then(|turn| {
                self.mirrors.values()
                    .find(|mirror| mirror.session_id.as_deref() == Some(session) && mirror.generation == generation)
                    .filter(|mirror| mirror.native.provider == "codex")
                    .map(|mirror| (mirror.native.clone(), turn.to_owned()))
            });
            let claude = receipt.ingress.as_ref().is_some_and(|ingress| ingress.native.provider == "claude");
            if receipt.outcome == "unknown" && (claude || turn.is_some()) {
                reconciles.ask(Ask { session: session.into(), request: request.into(), generation: generation.into(), receipt, turn });
                return Ok(());
            }
            self.acknowledge_workspace(session, generation, &receipt)?;
            return send(socket, &receipt_wire(session, request, &receipt)).await;
        };
        let mut receipt = self.command(&cmd, anchor).await?;
        if receipt.outcome == "unknown" && !receipt.settled {
            if let Some((native, turn)) = expected_turn.and_then(|turn| {
                self.mirrors.values()
                    .find(|mirror| mirror.session_id.as_deref() == Some(session) && mirror.generation == generation)
                    .map(|mirror| (mirror.native.clone(), turn))
            }) {
                if native.provider == "codex" && self.verified(&native).await.is_ok()
                    && native.transcript.as_ref().is_some_and(|path| codex::turn_finished(path, turn))
                {
                    receipt = Journal { dir: self.dir.join("receipts") }.settle(session, request)?;
                }
            }
        }
        send(socket, &receipt_wire(session, request, &receipt)).await
    }

    async fn reconcile(
        &mut self,
        session: &str,
        request: &str,
        generation: &str,
    ) -> Result<Receipt> {
        let journal = Journal {
            dir: self.dir.join("receipts"),
        };
        private_dir(&journal.dir)?;
        let mut receipt = journal.reconcile(session, request, generation)?;
        if receipt.outcome == "unknown" {
            if let Some(ingress) = &receipt.ingress {
                if ingress.native.provider == "claude"
                    && self.verified(&ingress.native).await.is_ok()
                {
                    if let Ok(Some(id)) = claude::admission(&self.home, ingress) {
                        receipt = journal.finish(session, request, Some(id), false)?;
                    }
                }
            }
        }
        self.acknowledge_workspace(session, generation, &receipt)?;
        Ok(receipt)
    }

    /// An applied send carried the workspace notice at its revision: the mirror stops owing it.
    fn acknowledge_workspace(&mut self, session: &str, generation: &str, receipt: &Receipt) -> Result<()> {
        if receipt.outcome == "applied" && !receipt.held {
            if let Some(revision) = receipt.ingress.as_ref().and_then(|i| i.workspace_revision) {
                if let Some((key, mirror)) = self.mirrors.iter_mut().find(|(_, m)| {
                    m.session_id.as_deref() == Some(session) && m.generation == generation
                }) {
                    let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                    mirror.workspace.acknowledge(revision);
                    persist_changed(&self.dir, key, mirror, &before)?;
                }
            }
        }
        Ok(())
    }

    async fn command(&mut self, cmd: &Command, anchor: Option<&ClockAnchor>) -> Result<Receipt> {
        let mut why = "";
        let result = self.command_inner(cmd, anchor, &mut why).await;
        // One line per command outcome, never its text: a silent "Not delivered" was undiagnosable.
        let outcome = match &result {
            Ok(receipt) => receipt.outcome.clone(),
            Err(error) => format!("error: {error}"),
        };
        relay_log(&format!(
            "command {} ({}) for {}: {outcome}{}{why}",
            short(&cmd.request_id),
            cmd.kind,
            short(&cmd.session_id),
            if why.is_empty() { "" } else { " -- " },
        ));
        result
    }

    async fn command_inner(
        &mut self,
        cmd: &Command,
        anchor: Option<&ClockAnchor>,
        why: &mut &'static str,
    ) -> Result<Receipt> {
        let journal = Journal {
            dir: self.dir.join("receipts"),
        };
        private_dir(&journal.dir)?;
        if matches!(cmd.kind.as_str(), "approve" | "answer") {
            return self.approve(&journal, cmd, anchor).await;
        }
        if journal
            .path(&cmd.session_id, &cmd.request_id)
            .try_exists()
            .map_err(|_| "journal unavailable")?
        {
            *why = "replayed request";
            journal.admit(cmd, "", 0)?; // Check immutable payload identity; never resend an intent.
            return self
                .reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation)
                .await;
        }
        let Some(key) = self
            .mirrors
            .iter()
            .find(|(_, m)| m.session_id.as_deref() == Some(&cmd.session_id))
            .map(|(k, _)| k.clone())
        else {
            return Err("unknown command session".into());
        };
        // Neither carries text into the session, so neither needs the workspace notice.
        let textless = matches!(cmd.kind.as_str(), "interrupt" | "end");
        if !textless { self.observe_workspace(&key).await?; }
        let Some(mirror) = self
            .mirrors
            .values()
            .find(|m| m.session_id.as_deref() == Some(&cmd.session_id))
        else {
            return Err("unknown command session".into());
        };
        let live = cmd.kind == "interrupt" || cmd.expected_turn_id.is_some();
        // This Mac's own human ending their own process shares nothing, so it is carried out for
        // a session that is not shared too ("Remove" on an Only me session). Nothing else is.
        let own_end = cmd.kind == "end" && cmd.own;
        // (`withheld` changes only between this loop's turns, so it holds until the dispatch.)
        let eligible = mirror.generation == cmd.expected_generation
            && mirror.registration.successor.is_none()
            && (own_end || !mirror.withheld())
            && !self.stop.load(Ordering::SeqCst)
            && anchor
                .and_then(ClockAnchor::now)
                .is_some_and(|now| cmd.authorized_at(now));
        let native = if !eligible {
            None
        } else if cmd.kind == "end" {
            // End needs this process to be the one that was verified (its pid and start time,
            // proved again before the stop below) and, for an interactive Claude, still on this
            // conversation (`/resume` and `/clear` switch it in place). A full verification runs
            // `claude --version` and lsof (1-3 s) and held every End back by that much.
            match end_identity(&mirror.native) {
                Some(native) if end_moved(&self.home, &crate::holder::manager::socket_dir(), &native) => {
                    *why = "session moved";
                    return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
                }
                native => native,
            }
        } else {
            self.verified(&mirror.native).await.ok()
        };
        let Some(native) = native else {
            *why = if eligible { "native session unverified" } else { "stale generation, expired, stopping or not shared" };
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        };
        // A fresh capture knows nothing of the holder Lightbulb runs the session in: that is the
        // mirror's. Without it a held Codex reads as one off its daemon and every send is refused.
        let native = NativeSession { holder: native.holder.clone().or_else(|| mirror.native.holder.clone()), ..native };
        if live && (native.provider != "codex" || native.status != "busy" || native.socket.is_none()
            || native.active_turn != cmd.expected_turn_id)
        {
            *why = "no matching active turn";
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        // Only this Mac's own human ends a session, and only one whose process is its own: one
        // Lightbulb holds, or a Claude (a background session is one process too, and Claude's
        // daemon stops it when asked). A Codex app or daemon process hosts many conversations.
        // A background session is stopped by its job id alone, never by a signal to a pid the
        // daemon hosts: one whose id is not a job id (also one verification dropped) is refused.
        let job = native.job.as_deref().or(mirror.native.job.as_deref());
        let no_job_id = job.is_some_and(|job| crate::holder::manager::attach_provider(job) != Some("claude"));
        if cmd.kind == "end" && (!cmd.own || (native.holder.is_none() && (native.provider != "claude" || no_job_id))) {
            *why = "this session's process is not its own to end";
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        // A Codex off its shared daemon (`codex --no-daemon`) owns its thread alone: `codex queue`
        // would hand the thread to the daemon, a second writer. Nothing delivers to it.
        if cmd.kind == "send" && native.terminal_reason() == Some("no-daemon") {
            *why = "no delivery path to a Codex off its shared daemon";
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        let mut socket = if native.provider == "claude" && cmd.kind != "end" {
            match claude::connect(&native).await {
                Ok(socket) => Some(socket),
                Err(_) => {
                    *why = "inbox socket unavailable";
                    return journal.reconcile(
                        &cmd.session_id,
                        &cmd.request_id,
                        &cmd.expected_generation,
                    )
                }
            }
        } else {
            None
        };
        // After the verification and the connect this turn awaited: its person may have pressed stop.
        if !own_end && self.stopped_now(&key) {
            *why = "sharing stopped";
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        let now = anchor.and_then(ClockAnchor::now).unwrap_or(u64::MAX);
        if !journal.admit(cmd, &mirror.generation, now)? {
            *why = "not admitted (order, shape or deadline)";
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        // Provenance is server-owned. JSON escaping keeps IDs from spoofing this boundary.
        let text = if textless { String::new() } else { cmd.native_text(&native.provider, &mirror.workspace.notice()) };
        let anchored = if textless { Ok(()) } else { journal.anchor(
            cmd, &native, text.clone(), Some(mirror.workspace.revision()),
        ) };
        if anchored.is_err()
            || self.stop.load(Ordering::SeqCst)
            || !anchor
                .and_then(ClockAnchor::now)
                .is_some_and(|now| cmd.authorized_at(now))
            || !process(native.process.pid).is_ok_and(|current| current == native.process)
        {
            *why = if anchored.is_err() { "intent not anchored" } else { "expired, stopping or process changed" };
            return journal.finish(&cmd.session_id, &cmd.request_id, None, false);
        }
        if cmd.kind == "end" {
            // The owner's End, from any device. A held session ends as the panel's End does: its
            // holder ends the provider and keeps the last picture; a plain Claude as closing its
            // terminal does (it saves and leaves on SIGTERM); a Claude background session (← moved
            // it to Claude's daemon, which keeps it running after its person's terminal is gone)
            // by `claude stop`, which keeps its conversation. The mirror then reports the end. The
            // check just above proved this pid is still the process that was verified.
            if native.holder.is_none() {
                // No holder to ask (`manager::end` notes its own): the end that follows was asked
                // for. Noted before the stop, which can end it before it returns; forgotten below
                // when no stop was sent.
                crate::holder::manager::end_asked_now(&cmd.session_id);
            }
            // ponytail: `claude stop` is awaited here, on the relay loop, for up to 5 s (a holder's
            // End for up to 2 s): no heartbeat goes meanwhile. Off-loop when that is ever seen.
            // A background job is stopped by Claude's daemon even when a holder of ours shows it
            // (`claude attach`): ending only that client left the job running with no row.
            let claude_job = job.filter(|job| native.provider == "claude" && crate::holder::manager::attach_provider(job) == Some("claude"));
            if let (Some(id), Some(_)) = (&native.holder, claude_job) {
                crate::holder::manager::end_asked_now(id);
            }
            let sent = match (native.holder.clone(), claude_job) {
                (_, Some(job)) => match provider_program(&self.home, "claude") {
                    Some(program) => native_output(&program, &["stop", job]).await.is_ok(),
                    None => false,
                },
                (Some(id), None) => tokio::task::spawn_blocking(move || crate::holder::manager::list().into_iter().find(|h| h.id == id)
                    .map(|holder| crate::holder::manager::end(&holder)).is_some()).await.unwrap_or(false),
                (None, None) => unsafe { libc::kill(native.process.pid as libc::pid_t, libc::SIGTERM) == 0 },
            };
            if !sent {
                match (&native.holder, claude_job) {
                    (None, _) => crate::holder::manager::end_asked_forget(&cmd.session_id),
                    (Some(id), Some(_)) => crate::holder::manager::end_asked_forget(id),
                    _ => {}
                }
            }
            return journal.finish(&cmd.session_id, &cmd.request_id, sent.then(|| cmd.request_id.clone()), false);
        }
        if live {
            let turn = cmd.expected_turn_id.as_deref().ok_or("active turn required")?;
            return match codex::control(&self.home, &native,
                if cmd.kind == "interrupt" { "interrupt" } else { "steer" }, turn,
                (cmd.kind == "send").then_some(text.as_str())).await {
                Ok(Some(id)) => journal.finish(&cmd.session_id, &cmd.request_id, Some(id), false),
                Ok(None) => journal.finish(&cmd.session_id, &cmd.request_id, None, false),
                Err(_) => journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation),
            };
        }
        // From this point failure/cancellation is unknown, never a retry/rejection.
        *why = "";
        // Cursor and Grok CLI: the letter waits for the session's next hook, and its receipt
        // stays unknown until that hook hands it over (`hooked::drain` settles it applied). An
        // agent that stopped taking turns (a usage limit) never told anyone it had the message.
        if super::hooked::provider(&native.provider) {
            let receipt = journal.path(&cmd.session_id, &cmd.request_id);
            let _ = super::hooked::deliver(&super::hooked::dir(), &native, &cmd.request_id, &text, Some(&receipt));
            return self.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation).await;
        }
        if let Some(socket) = &mut socket {
            let from = claude::sender_handle(cmd.own, cmd.label.as_ref().and_then(|l| l.handle.as_deref()));
            let _ = claude::send(socket, &text, &from).await;
            let anchored = journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation)?;
            if let Some(ingress) = &anchored.ingress {
                if let Some(id) = claude::await_admission(&self.home, ingress).await {
                    journal.finish(&cmd.session_id, &cmd.request_id, Some(id), false)?;
                }
            }
        } else if let Ok(Some(id)) = codex::send(&native, &text).await {
            return journal.finish(&cmd.session_id, &cmd.request_id, Some(id), true);
        }
        self.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation)
            .await
    }
}

impl Host {
    /// A remote answer to the prompt this Mac is holding. Delivered only to the
    /// hook holding that exact prompt, in this generation, for a command the
    /// backend admitted; the hook re-checks id and digest before it answers.
    async fn approve(
        &mut self,
        journal: &Journal,
        cmd: &Command,
        anchor: Option<&ClockAnchor>,
    ) -> Result<Receipt> {
        if journal
            .path(&cmd.session_id, &cmd.request_id)
            .try_exists()
            .map_err(|_| "journal unavailable")?
        {
            journal.admit(cmd, "", 0)?; // identity check; never answer twice
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        let Some(key) = self
            .mirrors
            .iter()
            .find(|(_, m)| m.session_id.as_deref() == Some(&cmd.session_id))
            .map(|(k, _)| k.clone())
        else {
            return Err("unknown command session".into());
        };
        let mirror = &self.mirrors[&key];
        let now = anchor.and_then(ClockAnchor::now).unwrap_or(u64::MAX);
        let held = mirror
            .waiting
            .as_ref()
            .and_then(|w| w.held.clone().filter(|held| if cmd.kind == "answer" {
                question_answerable(w, held, cmd)
            } else { w.tool.as_deref() != Some("AskUserQuestion") && answerable(held, w.shipped_full, cmd) }));
        // A Codex hold is answered over its daemon (`codex::answer`), named by the request's item.
        let codex = held
            .as_ref()
            .is_some_and(|held| mirror.native.provider == "codex" && daemon_held(held))
            .then(|| (mirror.native.clone(), mirror.waiting.as_ref().and_then(|w| w.id.clone())));
        let live = mirror.generation == cmd.expected_generation
            && mirror.registration.successor.is_none()
            && mirror.ended_at.is_none()
            && !mirror.withheld()
            && !self.stop.load(Ordering::SeqCst)
            && cmd.authorized_at(now)
            && (if cmd.kind == "answer" {
                question_runtime(&mirror.native, process(mirror.native.process.pid).ok().as_ref())
            } else {
                self.verified(&mirror.native).await.is_ok()
            })
            && !self.stopped_now(&key);
        let mirror = &self.mirrors[&key];
        if !journal.admit(cmd, &mirror.generation, now)? {
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        let confined = |held: &approval::Held| {
            codex.is_some()
                || enrollment::hook_path()
                    .parent()
                    .is_some_and(|dir| approval::reply_allowed(dir, &held.reply, &held.id))
        };
        let Some(held) = held.filter(|held| live && confined(held)) else {
            return journal.finish(&cmd.session_id, &cmd.request_id, None, false);
        };
        let answer = approval::Answer {
            approval_id: held.id.clone(),
            input_digest: held.input_digest.clone(),
            decision: if cmd.kind == "answer" { "answer".into() } else { cmd.decision.clone().unwrap_or_default() },
            answers: cmd.answers.clone(),
            mode: cmd.mode.clone(),
            message: (cmd.decision.as_deref() == Some("deny")).then(|| {
                match cmd.text.as_deref().map(str::trim).filter(|t| !t.is_empty()) {
                    Some(reason) => format!("Denied from Lightbulb: {reason}"),
                    None => "Denied from Lightbulb.".to_owned(),
                }
            }),
        };
        let delivered = match &codex {
            Some((native, Some(item))) => codex::answer(&self.home, native, item, &held.input_digest, &answer.decision, cmd.mode.as_deref())
                .await
                .unwrap_or(false),
            Some((_, None)) => false,
            None => approval::deliver(&held.reply, Some(&answer)),
        };
        if !delivered {
            return journal.finish(&cmd.session_id, &cmd.request_id, None, false);
        }
        // Answered: the prompt is gone and the session works (or is told no).
        let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
        let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
        mirror.waiting = None;
        mirror.native.status = "busy".into();
        persist_changed(&self.dir, &key, mirror, &before)?;
        self.heartbeat_dirty.insert(key);
        journal.finish(&cmd.session_id, &cmd.request_id, Some(held.id), false)
    }
}

/// A Codex thread's open approval, held the way a Claude hook holds a prompt (spec 2026-10-26 (f),
/// gap 12). Its daemon replays it to any client that joins the thread, so the app answers it over
/// the app-server (`codex::answer`), with no hook in between. Discovery owns the hold: once the
/// daemon no longer holds the request (answered in the terminal, the turn moved on, sharing
/// stopped), the prompt is let go.
fn codex_hold(mirror: &mut Mirror, withheld: bool) {
    if mirror.native.provider != "codex" || mirror.ended_at.is_some() {
        return;
    }
    let thread = mirror.native.conversation.clone();
    let open = codex::open_request(&thread)
        .filter(|_| mirror.native.status == "waiting-approval" && !withheld)
        .and_then(|request| codex::approval(&request));
    let Some(open) = open else {
        if mirror.waiting.as_ref().and_then(|w| w.held.as_ref()).is_some_and(daemon_held) {
            mirror.waiting = None;
        }
        return;
    };
    let id = codex::approval_id(&thread, &open.item);
    if mirror.waiting.as_ref().and_then(|w| w.held.as_ref()).is_some_and(|h| h.id == id) {
        return;
    }
    let summary = redact(open.summary.trim());
    // Exact as a Claude shell prompt is: the summary IS the whole command line.
    let exact = summary == open.summary.trim() && approval::exact(open.tool, &open.input, None, None);
    let held = approval::Held {
        id,
        input_digest: approval::digest(&open.input),
        reply: CODEX_DAEMON.into(),
        exact,
        modes: if open.session { vec!["session".into()] } else { vec![] },
        full: open.full,
        similar: None,
    };
    let prompt = WaitingPrompt {
        id: Some(open.item),
        tool: Some(open.tool.into()),
        summary: summary.clone(),
        held: Some(held),
        private_send: None,
        input: open.full.then_some(open.input),
        shipped_full: false,
    };
    mirror.queue.push(approval_row(&summary, &prompt));
    mirror.waiting = Some(prompt);
}

/// The reply "channel" of a prompt Codex's daemon holds (`codex_hold`): it has no hook socket.
/// Never a path `approval::reply_allowed` admits, so no hook report can carry it in.
const CODEX_DAEMON: &str = "codex-daemon";

fn daemon_held(held: &approval::Held) -> bool {
    held.reply == CODEX_DAEMON
}

/// A complete held question proves its own input capability. Reading older history or
/// the engine's ordinary-send capability is unnecessary; the provider process incarnation
/// and still-held private hook channel remain mandatory.
fn question_runtime(native: &NativeSession, current: Option<&Process>) -> bool {
    native.provider == "claude" && native.status != "ended"
        && matches!(native.restriction.as_deref(), None | Some("history-gap" | "runtime-unverified"))
        && current == Some(&native.process)
}

fn question_answerable(prompt: &WaitingPrompt, held: &approval::Held, cmd: &Command) -> bool {
    prompt.tool.as_deref() == Some("AskUserQuestion") && prompt.shipped_full && held.full
        && cmd.kind == "answer" && cmd.text.is_none() && cmd.mode.is_none() && cmd.decision.is_none()
        && cmd.approval_id.is_none() && cmd.expected_turn_id.is_none()
        && cmd.prompt_id.as_deref() == Some(held.id.as_str())
        && cmd.input_digest.as_deref() == Some(held.input_digest.as_str())
        && cmd.actor.is_none() && cmd.actor_user_id.as_deref().is_some_and(|u| !u.is_empty())
        && prompt.input.as_ref().zip(cmd.answers.as_ref()).is_some_and(|(i,a)| approval::question_answers(i,a))
}

/// Does this command answer exactly this held prompt, with an option it offered?
fn answerable(held: &approval::Held, shipped_full: bool, cmd: &Command) -> bool {
    // A person's decision only: an agent never answers a permission prompt.
    cmd.actor.is_none()
        && cmd.actor_user_id.as_deref().is_some_and(|u| !u.is_empty())
        && cmd.approval_id.as_deref() == Some(held.id.as_str())
        && cmd.input_digest.as_deref() == Some(held.input_digest.as_str())
        && match cmd.decision.as_deref() {
            // `full`: the viewer was shown the whole call, and the digest names it.
            Some("allow") => (held.exact || (held.full && shipped_full)) && cmd.mode.as_ref().is_none_or(|m| held.modes.contains(m)),
            Some("deny") => cmd.mode.is_none(),
            _ => false,
        }
}

/// The transcript row a prompt leaves, tagged with how to answer it when the
/// hook is holding it (the backend reads the tag; see `pending_approval`).
fn approval_row(text: &str, prompt: &WaitingPrompt) -> Value {
    let Some(held) = &prompt.held else {
        return lifecycle("approval_pending", text, None, None);
    };
    let mut row = lifecycle("approval_pending", text, None, Some(&held.id));
    row["tool"] = json!({
        "name": prompt.tool.as_deref().unwrap_or("tool"),
        "status": "running",
        "input": {
            "summary": bounded(&prompt.summary, 300).0,
            "inputDigest": held.input_digest,
            "exact": held.exact && prompt.private_send.is_none(),
            "modes": held.modes,
        },
    });
    if prompt.tool.as_deref() == Some("AskUserQuestion") {
        row["tool"]["input"]["question"] = json!(true);
        row["tool"]["input"]["promptId"] = json!(held.id);
        row["tool"]["input"]["exact"] = json!(false);
        row["tool"]["input"]["modes"] = json!([]);
    }
    if let Some(similar) = &held.similar {
        row["tool"]["input"]["similar"] = json!(similar);
    }
    row
}

fn parse_command(body: &Value, human: &str) -> Result<Command> {
    let mut command: Command =
        serde_json::from_value(body["command"].clone()).map_err(|_| "invalid native command")?;
    command.label = serde_json::from_value::<ActorLabel>(body["actor"].clone()).ok();
    command.own = command.actor.is_none() && command.actor_user_id.as_deref() == Some(human);
    Ok(command)
}

fn receipt_wire(session: &str, request: &str, receipt: &Receipt) -> Value {
    json!({"type":"local.session.receipt","sessionId":session,"requestId":request,"generation":receipt.generation,
        "outcome":receipt.outcome,"providerMessageId":receipt.provider_message_id,"held":receipt.held,"settled":receipt.settled})
}

/// The mirror key of this Mac's Claude conversation, as `native_key` of its session.
pub(super) fn claude_key(home: &Path, conversation: &str) -> String {
    key("claude", &claude::profile(home), conversation)
}

fn native_key(native: &NativeSession) -> String {
    key(&native.provider, &native.profile, &native.conversation)
}

fn key(provider: &str, profile: &str, conversation: &str) -> String {
    fingerprint(&json!([provider, profile, conversation]).to_string())
}

fn epoch_secs() -> u64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or_default()
}

/// The stored identity has to still be at that pid: pids are reused, and
/// `kill(pid, 0)` would call the stranger that inherited it this session alive
/// forever. `process` is a `proc_pidinfo` read -- no spawn, no signal.
fn still_running(identity: &Process) -> bool {
    process(identity.pid).is_ok_and(|current| &current == identity)
}

fn transcript_unread(mirror: &Mirror) -> bool {
    if mirror.ended_sent || mirror.native.restriction.as_deref() == Some("history-gap") {
        return false;
    }
    let parent_unread = mirror
        .native
        .transcript
        .as_deref()
        .is_some_and(|path| transcript_path_unread(path, &mirror.cursor));
    let subagents_supported = !mirror.subagents.relay_known || mirror.subagents.relay;
    let within_retention = mirror.ended_at.is_some_and(|ended_at| {
        epoch_secs().saturating_sub(ended_at) < ENDED_RETENTION_SECS
    });
    // A Claude sub-agent runs inside its session's own process: once that process is gone it
    // can write nothing more, and what it did write is the unread check below. Waiting on it
    // held the end back for a day and kept a dead session listed as idle (prod 2026-10-07).
    let child_can_still_write = mirror.native.provider != "claude" || still_running(&mirror.native.process);
    let unfinished_child = within_retention && child_can_still_write && mirror
        .subagents
        .known
        .values()
        .any(|subagent| !subagent.closed && !subagent.ended);
    let untracked_claude_child = subagents_supported
        && within_retention
        && mirror.native.provider == "claude"
        && mirror.native.transcript.as_deref().is_some_and(|path| {
            !claude::new_subagents(path, &|id| mirror.subagents.known.contains_key(id)).is_empty()
        });
    parent_unread
        || subagents_supported
            && (unfinished_child
                || untracked_claude_child
                || mirror.subagents.known.values().any(|subagent| {
                    !subagent.closed
                        && subagent
                            .path
                            .as_deref()
                            .is_some_and(|path| transcript_path_unread(path, &subagent.cursor))
                }))
}

fn transcript_path_unread(path: &Path, cursor: &Cursor) -> bool {
    let Ok(mut file) = secure_file(path) else { return false };
    let Ok(metadata) = file.metadata() else { return false };
    if cursor.inode.is_some_and(|inode| inode != metadata.ino()) || metadata.len() < cursor.offset {
        return true;
    }
    if metadata.len() == cursor.offset || file.seek(SeekFrom::Start(cursor.offset)).is_err() {
        return false;
    }
    let mut bytes = Vec::new();
    file.take((MAX_JSONL_ROW + 1) as u64)
        .read_to_end(&mut bytes)
        .is_ok_and(|_| bytes.contains(&b'\n') || bytes.len() > MAX_JSONL_ROW)
}

fn has_drainable_backlog(mirror: &Mirror) -> bool {
    // What a withheld session wrote stays on this Mac: its end is reported with nothing drained.
    !mirror.withheld() && (mirror.pending.is_some() || !mirror.queue.is_empty() || transcript_unread(mirror))
}

/// A Codex Desktop thread can outlive its SessionEnd hook in the same app-server.
/// Discovery gets the first chance to reopen an actually running turn before reconnect
/// recovery treats the old end as final.
fn codex_turn_active_after_end(mirror: &Mirror) -> bool {
    let Some(ended_at) = mirror.ended_at else { return false };
    if mirror.native.provider != "codex" || !still_running(&mirror.native.process) {
        return false;
    }
    let Some(path) = mirror.native.transcript.as_deref() else { return false };
    // A task may already be complete by the time this reconnect is checked. The
    // post-end task_started is still evidence for discovery to revive it; using
    // the current busy/idle state here would discard its completed output.
    codex::active_turn_started_after(path, ended_at, mirror.ended_at_ms)
}

fn needs_ended_recovery(mirror: &Mirror) -> bool {
    mirror.ended_at.is_some()
        && !mirror.ended_sent
        && has_drainable_backlog(mirror)
        && !codex_turn_active_after_end(mirror)
}

/// An ended run the relay has never heard of, bound to a remote start: it registers once,
/// with nothing to drain, so its end has a session to land on. True only until that ack.
fn owes_bound_register(dir: &Path, mirror: &Mirror) -> bool {
    mirror.ended_at.is_some()
        && !mirror.ended_sent
        && !mirror.withheld()
        && mirror.session_id.is_none()
        && start_links::bound(dir, &mirror.native, &mirror.generation).is_some()
}

fn ended_recovery_keys(mirrors: &BTreeMap<String, Mirror>) -> BTreeSet<String> {
    mirrors
        .iter()
        .filter(|(_, mirror)| needs_ended_recovery(mirror))
        .map(|(key, _)| key.clone())
        .collect()
}

/// Retry the exact durable cursor; never reset it or skip an unread record. A
/// partial final record is not ready. Verified EOF can recover without new output.
fn readable_history(mirror: &Mirror) -> bool {
    mirror.native.transcript.as_deref().is_some_and(|path| {
        let mut probe = mirror.cursor.clone();
        probe.read(path, &mirror.native.provider).is_ok_and(|rows| {
            !rows.is_empty()
                || secure_file(path)
                    .and_then(|file| file.metadata().map_err(|e| e.to_string()))
                    .is_ok_and(|meta| Some(meta.ino()) == probe.inode && meta.len() == probe.offset)
        })
    })
}

/// A read failure is a history problem only. A process that is gone is ended by `liveness_tick`,
/// a Claude one by `apply_engine`: they own that verdict.
fn capture_batch(mirror: &mut Mirror) -> Result<Option<Value>> {
    mirror.next_batch().inspect_err(|reason| {
        mirror.native.restriction = Some(
            if reason == "runtime-unverified" {
                "runtime-unverified"
            } else {
                "history-gap"
            }
            .into(),
        );
    })
}
fn persist_changed(dir: &Path, key: &str, mirror: &mut Mirror, before: &Value) -> Result<()> {
    if mirror.dirty || &serde_json::to_value(&*mirror).map_err(|e| e.to_string())? != before {
        mirror.persist(&dir.join(format!("{key}.json")))?;
    }
    Ok(())
}

fn registration(mirror: &Mirror) -> Value {
    let mut body = json!({"type":"local.session.register", "session":{"provider":mirror.native.provider,
        "profileFingerprint":mirror.native.profile,"nativeConversationId":mirror.native.conversation,"generation":mirror.generation,
        "providerVersion":mirror.native.version,"status":mirror.native.status,"activeTurnId":mirror.native.active_turn,"controlUnavailableReason":mirror.native.control_unavailable_reason(),
        "title":mirror.native.title,"cwdName":mirror.native.cwd_name,"holderId":mirror.native.holder,
        "workspace":mirror.workspace.summary()}});
    if mirror.withheld() {
        body["session"]["shared"] = json!(false);
    }
    super::session_states::add_attention(&mut body["session"], &mirror.native);
    super::session_states::add_network_id(&mut body["session"], mirror);
    if let Some(previous) = &mirror.previous_generation {
        body["previousGeneration"] = json!(previous);
    }
    body
}

/// An End's check of the session it stops: still live and unrestricted, and its process the one
/// verified before (pid and start time). The cheap half of `verify_identity`.
fn end_identity(native: &NativeSession) -> Option<NativeSession> {
    (native.status != "ended" && native.restriction.is_none()).then(|| super::hooked::verify(native).ok()).flatten()
}

/// Whether the process an End would stop left this row's conversation in place: an interactive
/// Claude switched by `/resume` or `/clear` (its pid record names another), or a held Codex TUI
/// switched by `/new` or `/resume` (its holder is bound to another thread). One small file each;
/// no record, no evidence.
fn end_moved(home: &Path, holders: &Path, native: &NativeSession) -> bool {
    match native.provider.as_str() {
        "claude" => native.job.is_none() && !claude::still_registered(home, native.process.pid, &native.conversation),
        "codex" => native.holder.as_deref()
            .and_then(|id| crate::holder::manager::bound_thread(holders, id))
            .is_some_and(|thread| thread != native.conversation),
        _ => false,
    }
}

/// `Host::verify` in production: `verify_identity`, timed.
fn verify_native(home: PathBuf, native: NativeSession) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<NativeSession>> + Send>> {
    Box::pin(async move {
        let started = Instant::now();
        let current = verify_identity(&home, &native).await;
        slow_log("verified()", &native.conversation, started);
        current
    })
}

async fn verify_identity(home: &Path, native: &NativeSession) -> Result<NativeSession> {
    if native.status == "ended" || native.restriction.is_some() {
        return Err("control unavailable".into());
    }
    if super::hooked::provider(&native.provider) {
        return super::hooked::verify(native);
    }
    let program = if native.provider == "codex" {
        codex::program(home)
    } else {
        provider_program(home, &native.provider)
    }
    .ok_or("runtime unavailable")?;
    let current = if native.provider == "claude" {
        claude::capture(
            home,
            &program,
            &json!({"kind": if native.job.is_some() {"background"} else {"interactive"}, "id": native.job,
            "pid":native.process.pid, "sessionId":native.conversation,"status":"unknown"}),
        )
        .await?
    } else {
        codex::capture(home, &program, native.process.pid)
            .await?
            .into_iter()
            .find(|s| s.conversation == native.conversation)
            .ok_or("native identity changed")?
    };
    if current.restriction.is_some()
        || current.process != native.process
        || current.conversation != native.conversation
        || current.profile != native.profile
        // None -> Some is the session's first turn writing its transcript, not a new identity.
        || (native.transcript.is_some() && current.transcript != native.transcript)
        || current.socket != native.socket
        || current.socket_inode != native.socket_inode
        || current.version != native.version
    {
        return Err("native identity changed".into());
    }
    Ok(current)
}

/// Every line starts with its UTC time: `2026-10-07T12:05:31.123Z [local-sessions] ...`.
pub(super) fn relay_log(msg: &str) {
    let ms = SystemTime::now().duration_since(SystemTime::UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64);
    crate::log(&format!("{} [local-sessions] {msg}", utc_stamp(ms)));
}

/// Unix milliseconds as `2026-10-07T12:05:31.123Z` (no date crate here; days to a civil date
/// by Howard Hinnant's `civil_from_days`).
fn utc_stamp(ms: u64) -> String {
    let (days, rest) = (ms / 86_400_000, ms % 86_400_000);
    let z = days + 719_468;
    let (era, doe) = (z / 146_097, z % 146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let (day, month) = (doy - (153 * mp + 2) / 5 + 1, if mp < 10 { mp + 3 } else { mp - 9 });
    let year = yoe + era * 400 + u64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        rest / 3_600_000, rest / 60_000 % 60, rest / 1000 % 60, rest % 1000
    )
}

/// One line when a native check took over a second: `verified() took 12.3s for 0a1b2c3d`.
pub(super) fn slow_log(what: &str, id: &str, started: Instant) {
    let took = started.elapsed();
    if took > Duration::from_secs(1) {
        relay_log(&format!("{what} took {:.1}s for {}", took.as_secs_f64(), short(id)));
    }
}

fn short(id: &str) -> &str {
    &id[..id.len().min(8)]
}

/// The next session owing a registration: still live, not already registered, not
/// quarantined. An ended session is never registered -- it is on its way out.
fn registration_due(
    mirrors: &BTreeMap<String, Mirror>,
    registered: &BTreeSet<String>,
    quarantined: &BTreeMap<String, Instant>,
) -> Option<String> {
    registration_due_with_recovery(mirrors, registered, quarantined, &BTreeSet::new())
}

fn registration_due_with_recovery(
    mirrors: &BTreeMap<String, Mirror>,
    registered: &BTreeSet<String>,
    quarantined: &BTreeMap<String, Instant>,
    recovering_ended: &BTreeSet<String>,
) -> Option<String> {
    mirrors
        .iter()
        .find(|(key, mirror)| {
            (mirror.ended_at.is_none() || recovering_ended.contains(*key))
                // A withheld session registers nothing, whatever it still waits on.
                && (!stopped(key, mirror) || paused(mirror))
                && !registered.contains(*key)
                && !quarantine_active(quarantined, key)
        })
        .map(|(key, _)| key.clone())
}

/// The mirror key a relay-supplied session id names, if this Mac still holds it.
fn session_key(mirrors: &BTreeMap<String, Mirror>, session: Option<&str>) -> Option<String> {
    let session = session?;
    mirrors
        .iter()
        .find(|(_, m)| m.session_id.as_deref() == Some(session))
        .map(|(key, _)| key.clone())
}

/// One device heartbeat listing every registered, still-running session, split
/// into as many frames as the relay's limits need. An ended session is never
/// listed: `ended` is not a heartbeat status and the relay would reject the whole
/// frame over it. A key whose mirror has been reaped is simply not there.
fn device_heartbeats(
    mirrors: &BTreeMap<String, Mirror>,
    registered: &BTreeSet<String>,
    with_stats: bool,
) -> Result<Vec<Value>> {
    device_heartbeats_with_controls(mirrors, registered, with_stats, false, false, false, false, false, false, false, false)
}

/// The mirrors forked from `origin_key`'s conversation (same provider and profile).
fn forks_of(mirrors: &BTreeMap<String, Mirror>, origin_key: &str) -> Vec<String> {
    let Some(origin) = mirrors.get(origin_key).map(|m| &m.native) else {
        return vec![];
    };
    mirrors
        .iter()
        .filter(|(_, m)| {
            m.native.forked_from.as_deref() == Some(origin.conversation.as_str())
                && m.native.provider == origin.provider
                && m.native.profile == origin.profile
        })
        .map(|(key, _)| key.clone())
        .collect()
}

/// The Lightbulb id of the session a fork continues, when this Mac mirrors it.
fn origin_session(mirrors: &BTreeMap<String, Mirror>, native: &NativeSession) -> Option<String> {
    let origin = native.forked_from.as_deref()?;
    mirrors
        .values()
        .find(|m| m.native.provider == native.provider && m.native.profile == native.profile && m.native.conversation == origin)?
        .session_id
        .clone()
}

fn device_heartbeats_with_controls(
    mirrors: &BTreeMap<String, Mirror>,
    registered: &BTreeSet<String>,
    with_stats: bool,
    with_controls: bool,
    live_terminal: bool,
    remote_input: bool,
    terminal: bool,
    presence: bool,
    pickups: bool,
    band_owner_only: bool,
    agent_asks: bool,
) -> Result<Vec<Value>> {
    // `liveTerminal`: this Mac takes `local.session.watch`; `remoteInput`: `local.session.input`.
    // Each only to a relay that lists it.
    let envelope = |sessions: &[Value]| {
        let mut frame = json!({"type":"local.device.heartbeat","sessions":sessions});
        if live_terminal {
            frame["liveTerminal"] = json!(true);
        }
        if remote_input {
            frame["remoteInput"] = json!(true);
            // This Mac's holders take the app panel's size with remote input: the session takes the
            // size of whoever used it last (owner ruling 2026-10-14). Only to a relay that lists it.
            if super::session_states::reads_remote_resize() {
                frame["remoteResize"] = json!(true);
            }
        }
        if presence {
            frame["sessionPresence"] = json!(true);
        }
        // This Mac's terminal names a pick-up request as one; without it the relay sends none.
        if pickups {
            frame["pickupRequests"] = json!(true);
        }
        // This Mac strips ctrl+x and mouse reports from a teammate's input (only the owner can
        // operate the band), so nothing freezes while a request waits. Without it the relay
        // tells the web its typing is paused.
        if band_owner_only {
            frame["bandOwnerOnly"] = json!(true);
        }
        // This Mac's band shows an agent's ask to message a session as one (spec
        // 2026-10-07-agent-session-approval). Part of the envelope, so the size limit counts it.
        if agent_asks {
            frame["agentMessageAsks"] = json!(true);
        }
        frame
    };
    let mut frames = vec![];
    let mut sessions: Vec<Value> = vec![];
    for (key, m) in mirrors.iter().filter(|(key, m)| {
        registered.contains(*key) && m.ended_at.is_none() && (!stopped(key, m) || paused(m)) && m.session_id.is_some()
    }) {
        let entry = json!({"sessionId":m.session_id,"generation":m.generation,
            "status":m.native.status,"activeTurnId":m.native.active_turn,
            "controlUnavailableReason":m.native.control_unavailable_reason(),
            "title":m.native.title,"cwdName":m.native.cwd_name,
            "lastAssistantMessage":if m.withheld() { None } else { m.native.last_assistant_message.as_deref() },
            "workspace":m.workspace.summary()});
        // Only to a relay that said it reads them: an older one rejects the whole frame.
        let mut entry = entry;
        if let Some(stats) = m.stats.wire().filter(|_| with_stats) {
            entry["stats"] = stats;
        }
        if with_controls {
            let live = m.native.provider == "codex"
                && m.native.version == "0.159.3"
                && m.native.socket.is_some() && m.native.socket_inode.is_some()
                && m.native.restriction.is_none();
            entry["capabilities"] = json!({"sendMode":"queue","steer":live,"interrupt":live});
        }
        if terminal {
            entry["terminal"] = json!(m.native.terminal());
            super::session_states::add_reason(&mut entry, &m.native);
        }
        super::session_states::add_attention(&mut entry, &m.native);
        super::session_states::add_network_id(&mut entry, m);
        super::signals::add(&mut entry, key, m);
        // This session's own holder takes the app panel's size (its HELLO said so): a holder an
        // older build started does not, though this Mac advertises remoteResize. Only to a relay
        // that lists remoteResize.
        if remote_input && super::session_states::reads_remote_resize()
            && m.native.holder.as_deref().is_some_and(crate::holder::manager::takes_resize)
        {
            entry["terminalResize"] = json!(true);
        }
        if ascii_wire_len(&envelope(std::slice::from_ref(&entry)))? > 16384 {
            // One session too big to travel even alone would make every frame it
            // lands in oversized, and `send` takes the connection down over it.
            // Leave it out; the rest of the Mac still gets heard.
            relay_log(&format!("{} heartbeat fields exceed one frame; omitted", short(key)));
            continue;
        }
        sessions.push(entry);
        if sessions.len() <= HEARTBEAT_SESSIONS_MAX
            && ascii_wire_len(&envelope(&sessions))? <= 16384
        {
            continue;
        }
        // Every entry fits alone, so what just overflowed always leaves a non-empty
        // frame behind and always starts a viable next one.
        let Some(overflow) = sessions.pop() else {
            continue;
        };
        frames.push(envelope(&sessions));
        sessions = vec![overflow];
    }
    if !sessions.is_empty() {
        frames.push(envelope(&sessions));
    }
    Ok(frames)
}

fn attach_private_send(mirror: &mut Mirror, mut batch: Value) -> Value {
    if batch.get("privateApproval").is_some() {
        return batch;
    }
    let Some(waiting) = mirror.waiting.as_ref() else {
        return batch;
    };
    let Some(held) = waiting.held.as_ref().filter(|held| held.exact) else {
        return batch;
    };
    let Some(detail) = waiting.private_send.as_ref() else {
        return batch;
    };
    if !batch["events"].as_array().is_some_and(|events| {
        events
            .iter()
            .any(|event| event["lifecycle"] == "approval_pending" && event["itemId"] == held.id)
    }) {
        return batch;
    }
    batch["privateApproval"] = detail.clone();
    while ascii_wire_len(&wire_batch(batch.clone())).is_ok_and(|len| len > 16384)
        && batch["events"]
            .as_array()
            .is_some_and(|events| events.len() > 1)
    {
        batch["events"].as_array_mut().unwrap().pop();
        if !batch["events"]
            .as_array()
            .unwrap()
            .iter()
            .any(|event| event["itemId"] == held.id)
        {
            batch.as_object_mut().unwrap().remove("privateApproval");
            break;
        }
    }
    if ascii_wire_len(&wire_batch(batch.clone())).is_ok_and(|len| len > 16384) {
        batch.as_object_mut().unwrap().remove("privateApproval");
    }
    mirror.pending = Some(batch.clone());
    batch
}

/// A relay that reads `approvalInput` gets the held prompt's row with the input it may be
/// allowed on (`full`). A row that would not fit drops `input` and is not `full`; failing that,
/// it goes alone in the next batch, and failing that it is exactly today's row.
fn attach_approval_input(mirror: &mut Mirror, mut batch: Value) -> Value {
    let Some(waiting) = mirror.waiting.as_ref() else {
        return batch;
    };
    let Some(held) = waiting.held.as_ref() else {
        return batch;
    };
    let Some(at) = batch["events"].as_array().and_then(|events| {
        events.iter().position(|event| {
            event["lifecycle"] == "approval_pending"
                && event["itemId"] == held.id
                && event["tool"]["input"].is_object()
                && event["tool"]["input"].get("full").is_none()
        })
    }) else {
        return batch;
    };
    let over = |batch: &Value| ascii_wire_len(&wire_batch(batch.clone())).map_or(true, |len| len > 16384);
    {
        let row = &mut batch["events"][at]["tool"]["input"];
        row["full"] = json!(held.full && waiting.input.is_some());
        if let Some(input) = &waiting.input {
            row["input"] = input.clone();
        }
    }
    if over(&batch) {
        batch["events"].as_array_mut().unwrap().truncate(at + 1);
    }
    if over(&batch) && at > 0 && waiting.tool.as_deref() == Some("AskUserQuestion") {
        // Keep the complete question queued for its own frame; unlike a permission
        // it has no useful deny-only form, and consuming a downgraded row strands it.
        batch["events"].as_array_mut().unwrap().truncate(at);
        if let Some(waiting) = mirror.waiting.as_mut() { waiting.shipped_full = false; }
        mirror.pending = Some(batch.clone());
        return batch;
    }
    if over(&batch) {
        let row = batch["events"][at]["tool"]["input"].as_object_mut().unwrap();
        row.remove("input");
        row.insert("full".into(), json!(false));
    }
    if over(&batch) && at > 0 {
        batch["events"].as_array_mut().unwrap().truncate(at);
    } else if over(&batch) {
        batch["events"][at]["tool"]["input"].as_object_mut().unwrap().remove("full");
    }
    let shipped = batch["events"].as_array().is_some_and(|events| {
        events.iter().any(|event| {
            event["itemId"] == held.id
                && event["tool"]["input"]["full"] == true
                && event["tool"]["input"].get("input").is_some()
        })
    });
    if let Some(waiting) = mirror.waiting.as_mut() {
        waiting.shipped_full = shipped;
    }
    mirror.pending = Some(batch.clone());
    batch
}

fn carries_approval_input(batch: &Value) -> bool {
    batch["events"].as_array().is_some_and(|events| {
        events
            .iter()
            .any(|event| event["lifecycle"] == "approval_pending" && event["tool"]["input"].get("full").is_some())
    })
}

fn wire_batch(mut batch: Value) -> Value {
    // ponytail: a batch an older build pinned to disk still carries the retired
    // room-reply marker; delete once no Mac can hold one from before this release.
    batch.as_object_mut().map(|fields| fields.remove("_roomReplyRequestId"));
    batch
}

/// A register or heartbeat ack's `features` lists `endedStartId`. No list is a relay older
/// than the feature.
fn reads_ended_start(ack: &Value) -> bool {
    ack["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "endedStartId"))
}

fn quarantine_active(quarantined: &BTreeMap<String, Instant>, key: &str) -> bool {
    quarantined
        .get(key)
        .is_some_and(|at| at.elapsed() < QUARANTINE)
}

fn quarantine(
    quarantined: &mut BTreeMap<String, Instant>,
    registered: &mut BTreeSet<String>,
    sent_batches: &mut BTreeMap<String, Instant>,
    key: &str,
    reason: &str,
) {
    relay_log(&format!(
        "{} quarantined for {}s: {reason}",
        short(key),
        QUARANTINE.as_secs()
    ));
    quarantined.insert(key.to_owned(), Instant::now());
    registered.remove(key);
    sent_batches.remove(key);
}

/// An ack with no request in flight answers one this Mac no longer waits for. Ending the
/// connection for it took every session offline; ignoring it costs nothing.
fn late_ack() {
    relay_log("ignored an acknowledgment that answers no request in flight");
}

/// The next relay frame, or the in-flight request's deadline (`ACK_DEADLINE`) running out. Biased: a loop
/// that overslept the deadline still reads an answer already in the socket buffer first.
async fn next_or_deadline<S: futures_util::Stream + Unpin>(
    socket: &mut S,
    deadline: Option<Instant>,
) -> Result<Option<S::Item>> {
    tokio::select! {
        biased;
        incoming = socket.next() => Ok(incoming),
        _ = tokio::time::sleep_until(deadline.unwrap_or_else(Instant::now).into()), if deadline.is_some() => {
            Err("session acknowledgment timed out".into())
        }
    }
}

/// The clock anchor an acknowledgment proves. A round trip over ten seconds (a stalled loop
/// read it late) or clocks that disagree prove nothing: the ACK still counts, and the
/// previous anchor stays.
fn ack_anchor(server: u64, sent: Instant, wall: SystemTime) -> Option<ClockAnchor> {
    let rtt = sent.elapsed().as_secs_f64();
    let wall_elapsed = SystemTime::now().duration_since(wall).ok().map(|d| d.as_secs_f64());
    if deadline_now(server, 0.0, rtt, wall_elapsed).filter(|_| rtt <= 10.0).is_none() {
        relay_log(&format!("stale time acknowledgment after {rtt:.1}s; clock anchor kept"));
        return None;
    }
    Some(ClockAnchor { server, round_trip: rtt, at: Instant::now(), wall: SystemTime::now() })
}

/// Times one connection-loop iteration. The loop reads the socket only in its `select!`,
/// so an iteration over a second delays every answer by as much; the log names its slowest step.
struct Laps {
    iteration: Instant,
    lap: Instant,
    slowest: (&'static str, Duration),
    /// How long the `select!` waited before an arm ran.
    waited: Duration,
}
/// The `select!` wait: its 100 ms tick bounds it, so a longer one is this task not being run.
const WAIT: &str = "waiting for an event";
impl Laps {
    fn new() -> Self {
        let now = Instant::now();
        Self { iteration: now, lap: now, slowest: ("", Duration::ZERO), waited: Duration::ZERO }
    }
    fn lap(&mut self, step: &'static str) {
        let took = self.lap.elapsed();
        if took > self.slowest.1 {
            self.slowest = (step, took);
        }
        self.lap = Instant::now();
    }
    /// First thing in a `select!` arm: the wait is its own lap, not part of `arm`'s.
    fn woke(&mut self, arm: &'static str) -> &'static str {
        self.waited = self.lap.elapsed();
        self.lap(WAIT);
        arm
    }
    /// Closes the iteration whose `select!` ran `arm`, then starts the next one.
    fn next(&mut self, arm: &'static str) {
        self.lap(arm);
        let lines = slow_iteration(self.iteration.elapsed(), self.slowest, self.waited);
        // Reset first: the time the log write takes belongs to the next iteration.
        *self = Self::new();
        for line in lines {
            relay_log(&line);
        }
    }
}

/// What a finished iteration logs: its slowest step when it took over a second, and that the
/// loop was not run at all when the `select!` wait alone passed five (prod 2026-10-07: App Nap
/// held the whole process for 112 s and the log blamed `tick`).
fn slow_iteration(took: Duration, slowest: (&'static str, Duration), waited: Duration) -> Vec<String> {
    let mut lines = vec![];
    if took > Duration::from_secs(1) {
        lines.push(format!(
            "connection loop iteration took {:.1}s; slowest step: {} ({:.1}s)",
            took.as_secs_f64(),
            slowest.0,
            slowest.1.as_secs_f64()
        ));
    }
    if waited > Duration::from_secs(5) {
        lines.push(format!(
            "connection loop not scheduled for {:.1}s (App Nap or a starved runtime)",
            waited.as_secs_f64()
        ));
    }
    lines
}

async fn send(socket: &mut Socket, body: &Value) -> Result<()> {
    if ascii_wire_len(body)? > 16384 {
        return Err("session frame exceeds limit".into());
    }
    tokio::time::timeout(
        Duration::from_secs(5),
        socket.send(Message::Text(body.to_string().into())),
    )
    .await
    .map_err(|_| "session send timed out")?
    .map_err(|_| "session send failed".into())
}

#[cfg(test)]
pub(super) mod tests {
    use super::super::engine_view::{EngineEnded, EngineRow};
    use super::*;

    #[tokio::test(flavor = "multi_thread")]
    async fn transport_a_stuck_workspace_scan_blocks_the_next_one() {
        static FLAG: AtomicBool = AtomicBool::new(false);
        let (release, stuck) = std::sync::mpsc::channel::<()>();
        let claim = ScanClaim::take(&FLAG).unwrap();
        // As in observe_workspace: the claim lives in the task, the blocking work never ends.
        let scan = tokio::spawn(async move {
            let _claim = claim;
            let _ = tokio::task::spawn_blocking(move || stuck.recv()).await;
        });
        assert!(tokio::time::timeout(Duration::from_millis(50), scan).await.is_err());
        assert!(ScanClaim::take(&FLAG).is_none(), "a second scan started beside a stuck one");
        drop(release);
        for _ in 0..200 {
            if let Some(next) = ScanClaim::take(&FLAG) {
                drop(next);
                return;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        panic!("the claim outlived the scan");
    }

    const SID: &str = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";
    const SID_B: &str = "bbbbcccc-dddd-4eee-8fff-000011112222";

    #[test]
    fn claude_key_is_the_key_of_a_captured_claude_session() {
        let home = std::path::Path::new("/Users/a");
        let native = NativeSession { provider: "claude".into(), profile: claude::profile(home), ..test_native(SID_B) };
        assert_eq!(claude_key(home, SID_B), native_key(&native));
    }

    fn test_native(conversation: &str) -> NativeSession {
        NativeSession {
            provider: "codex".into(),
            conversation: conversation.into(),
            profile: "f".repeat(64),
            process: process(std::process::id()).unwrap(),
            transcript: None,
            socket: None,
            socket_inode: None,
            version: "0.149.0".into(),
            restriction: None,
            status: "unknown".into(),
            title: None,
            cwd_name: None,
            active_turn: None,
            last_assistant_message: None,
            holder: None,
            job: None,
            forked_from: None,
            forked_by_person: None,
            aliases: vec![],
            app: true,
            attention: Default::default(),
        }
    }

    pub(in crate::local_sessions) fn test_mirror(conversation: &str, generation: &str) -> Mirror {
        Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native: test_native(conversation),
            generation: generation.into(),
            previous_generation: None,
            session_id: None,
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            shared: true,
            stop_owed: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
            network_id: None,
        }
    }

    #[test]
    fn native_live_controls_are_advertised_only_after_feature_ack_and_exact_codex_socket() {
        let mut mirror = test_mirror("thread-1", "generation-1");
        mirror.session_id = Some(SID.into());
        mirror.native.version = "0.159.3".into();
        mirror.native.status = "busy".into();
        mirror.native.active_turn = Some("turn-1".into());
        mirror.native.socket = Some(PathBuf::from("/private/tmp/codex-daemon-501/socket"));
        mirror.native.socket_inode = Some(7);
        let key = native_key(&mirror.native);
        let mirrors = BTreeMap::from([(key.clone(), mirror.clone())]);
        let registered = BTreeSet::from([key]);
        assert!(device_heartbeats(&mirrors, &registered, false).unwrap()[0]["sessions"][0].get("capabilities").is_none());
        assert!(device_heartbeats(&mirrors, &registered, false).unwrap()[0].get("liveTerminal").is_none(), "only to a relay that lists it");
        assert!(device_heartbeats(&mirrors, &registered, false).unwrap()[0].get("remoteInput").is_none(), "only to a relay that lists it");
        let frames = device_heartbeats_with_controls(&mirrors, &registered, false, true, false, false, false, false, false, false, false).unwrap();
        let entry = &frames[0]["sessions"][0];
        assert_eq!(entry["capabilities"], json!({"sendMode":"queue","steer":true,"interrupt":true}));
        mirror.native.socket = None;
        mirror.native.socket_inode = None;
        let key = native_key(&mirror.native);
        let frames = device_heartbeats_with_controls(&BTreeMap::from([(key.clone(), mirror.clone())]), &BTreeSet::from([key]), false, true, false, false, false, false, false, false, false).unwrap();
        assert_eq!(frames[0]["sessions"][0]["capabilities"], json!({"sendMode":"queue","steer":false,"interrupt":false}));
        mirror.native.socket = Some(PathBuf::from("/private/tmp/codex-daemon-501/socket"));
        mirror.native.socket_inode = Some(7);
        mirror.native.provider = "claude".into();
        let key = native_key(&mirror.native);
        let frames = device_heartbeats_with_controls(&BTreeMap::from([(key.clone(), mirror)]), &BTreeSet::from([key]), false, true, false, false, false, false, false, false, false).unwrap();
        let entry = &frames[0]["sessions"][0];
        assert_eq!(entry["capabilities"], json!({"sendMode":"queue","steer":false,"interrupt":false}));
    }

    fn test_host(dir: &Path, mirrors: Vec<(&str, Mirror)>) -> Host {
        Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.into(),
            home: dir.into(),
            mirrors: mirrors.into_iter().map(|(k, m)| (k.to_string(), m)).collect(),
            stop: Arc::new(AtomicBool::new(false)),
            hooks: None,
            rediscover: None,
            ended_pending: vec![],
            relay_reads_ended_start: None,
            engine: None,
            observed_at: None,
            discovery_read_ms: None,
            engine_missed: BTreeSet::new(),
            left_on_purpose: BTreeSet::new(),
            engine_woken: BTreeSet::new(),
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
            verify: verify_native,
            share_every: Some(true),
        }
    }

    #[test]
    fn local_sessions_a_session_is_shared_only_when_lightbulb_started_it_or_its_person_shares_it() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![]);
        host.share_every = Some(false); // Settings → "Share every new session" is off
        let outside = test_native("5eed0001-0000-4000-8000-000000000001");
        let mut held = test_native("5eed0002-0000-4000-8000-000000000002");
        held.holder = Some("ab12cd34".into());
        held.transcript = Some(dir.path().join("held.jsonl"));
        // A held terminal nobody prompted: Claude starts a job from it to take another session's message.
        let mut idle_held = test_native("5eed0005-0000-4000-8000-000000000005");
        idle_held.holder = Some("ab12cd35".into());
        let mut peer_job = test_native("5eed0006-0000-4000-8000-000000000006");
        peer_job.forked_from = Some(idle_held.conversation.clone());
        // ← out of a session that is shared, and out of one that is not.
        let mut fork = test_native("5eed0003-0000-4000-8000-000000000003");
        fork.forked_from = Some(held.conversation.clone());
        fork.forked_by_person = Some(true);
        let mut private_fork = test_native("5eed0004-0000-4000-8000-000000000004");
        private_fork.forked_from = Some(outside.conversation.clone());
        private_fork.forked_by_person = Some(true);
        // A peer's message moved the shared session on (its continuation's first prompt is the peer's).
        let mut peer_fork = test_native("5eed0007-0000-4000-8000-000000000007");
        peer_fork.forked_from = Some(held.conversation.clone());
        host.observe(vec![outside.clone(), held.clone(), idle_held.clone()]).unwrap();
        host.observe(vec![outside.clone(), held.clone(), idle_held.clone(), fork.clone(), private_fork.clone(), peer_job.clone(), peer_fork.clone()]).unwrap();
        let shared: Vec<bool> = [&outside, &held, &fork, &private_fork, &peer_job, &peer_fork].iter().map(|n| host.mirrors[&native_key(n)].shared).collect();
        assert_eq!(shared, [false, true, true, false, false, false], "a job out of a terminal nobody prompted, or moved on by a peer, is not the person's");
        // The continuation remembers the id it had before.
        assert_eq!(host.mirrors[&native_key(&fork)].native.aliases, [held.conversation.clone()]);
        // Undecided (no turn of its own yet) is private, also once the relay has heard of it; its
        // person's first turn then shares it and registers it again. A peer's never does.
        let peer_key = native_key(&peer_fork);
        host.mirrors.get_mut(&peer_key).unwrap().session_id = Some("5eed0007-5e55-4000-8000-000000000007".into());
        let mut by_person = peer_fork.clone();
        by_person.forked_by_person = Some(true);
        let invalidated = host.observe(vec![held.clone(), by_person]).unwrap();
        assert!(host.mirrors[&peer_key].shared && invalidated.contains(&peer_key));
        // The same while its first, private register is still in flight (no id yet).
        let mut late = test_native("5eed0008-0000-4000-8000-000000000008");
        late.forked_from = Some(held.conversation.clone());
        host.observe(vec![held.clone(), late.clone()]).unwrap();
        let late_key = native_key(&late);
        assert!(!host.mirrors[&late_key].shared);
        host.mirrors.get_mut(&late_key).unwrap().registration.pending = true;
        late.forked_by_person = Some(true);
        let invalidated = host.observe(vec![held.clone(), late]).unwrap();
        assert!(host.mirrors[&late_key].shared && invalidated.contains(&late_key), "its private register must go again");

        // Only its person's and never shared: it registers, heartbeats and uploads nothing, and takes nothing.
        let key = native_key(&outside);
        let sid = "5eed0001-5e55-4000-8000-000000000001";
        let shared_keys: BTreeSet<String> = host.mirrors.iter().filter(|(_, m)| m.shared).map(|(k, _)| k.clone()).collect();
        assert_eq!(registration_due_with_recovery(&host.mirrors, &shared_keys, &BTreeMap::new(), &BTreeSet::new()), None);
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.session_id = Some(sid.into()); // as if the relay had heard of it before
        mirror.queue.push(json!({"kind":"text"}));
        assert_eq!(mirror.next_batch().unwrap(), None);
        assert!(!has_drainable_backlog(mirror));
        let every: BTreeSet<String> = host.mirrors.keys().cloned().collect();
        let listed = |host: &Host| {
            device_heartbeats(&host.mirrors, &every, false).unwrap().iter().any(|f| f["sessions"].as_array().is_some_and(|s| s.iter().any(|e| e["sessionId"] == sid)))
        };
        // Known to the relay (shared once): it stays in its owner's list as Only you, with no text.
        let entry = |host: &Host| device_heartbeats(&host.mirrors, &every, false).unwrap()[0]["sessions"].as_array().unwrap().iter().find(|e| e["sessionId"] == sid).cloned();
        assert!(entry(&host).is_some_and(|e| e["lastAssistantMessage"].is_null()));
        assert_eq!(registration(&host.mirrors[&key])["session"]["shared"], false);
        assert_eq!(registration_due_with_recovery(&host.mirrors, &BTreeSet::new(), &BTreeMap::new(), &BTreeSet::new()).as_deref(), Some(key.as_str()));
        assert!(host.refuses_frame(&json!({"sessionId": sid})), "no terminal frame, watch or typed input");

        // Its person shares it: the whole session, from its start. It registers again.
        ask_share(&key, true);
        assert_eq!(asked(&key), Some((true, false)));
        assert_eq!(host.share_tick(), vec![key.clone()]);
        assert_eq!(asked(&key), None);
        assert!(host.mirrors[&key].shared && host.mirrors[&key].cursor.offset == 0);
        assert!(listed(&host) && !host.refuses_frame(&json!({"sessionId": sid})));
        assert!(registration(&host.mirrors[&key])["session"].get("shared").is_none(), "absent: the owner's own choice stands");
        assert!(host.mirrors.get_mut(&key).unwrap().next_batch().unwrap().is_some());
        assert!(private_read::<Mirror>(&dir.path().join(format!("{key}.json"))).unwrap().shared, "durable");

        // And stops: at once, and a holder found for it later does not undo that.
        ask_share(&key, false);
        assert!(host.share_tick().is_empty());
        assert!(host.mirrors[&key].withheld() && listed(&host), "paused: still its owner's, as Only you");
        let mut found = outside.clone();
        found.holder = Some("cd34ab12".into());
        host.observe(vec![found]).unwrap();
        assert!(!host.mirrors[&key].shared, "its person stopped sharing it");
    }

    #[test]
    fn local_sessions_end_sees_a_conversation_that_moved_out_of_its_process() {
        let home = tempfile::tempdir().unwrap();
        let holders = home.path().join("h");
        fs::create_dir_all(home.path().join(".claude/sessions")).unwrap();
        fs::create_dir_all(&holders).unwrap();
        const THREAD: &str = "019a1b2c-0000-7000-8000-000000000000";
        const OTHER: &str = "019a1b2c-0000-7000-8000-0000000000ff";
        let mut codex = test_native(THREAD);
        codex.provider = "codex".into();
        codex.holder = Some("c0dec0de".into());
        assert!(!end_moved(home.path(), &holders, &codex), "no bound thread: no evidence");
        fs::write(holders.join("c0dec0de.thread"), THREAD).unwrap();
        assert!(!end_moved(home.path(), &holders, &codex));
        fs::write(holders.join("c0dec0de.thread"), OTHER).unwrap();
        assert!(end_moved(home.path(), &holders, &codex), "/new or /resume bound the holder to another thread");
        codex.holder = None;
        assert!(!end_moved(home.path(), &holders, &codex), "not held: the full verification decides");

        let mut claude = test_native(THREAD);
        claude.provider = "claude".into();
        let record = home.path().join(format!(".claude/sessions/{}.json", claude.process.pid));
        fs::write(&record, json!({"sessionId": THREAD}).to_string()).unwrap();
        assert!(!end_moved(home.path(), &holders, &claude));
        fs::write(&record, json!({"sessionId": OTHER}).to_string()).unwrap();
        assert!(end_moved(home.path(), &holders, &claude));
        claude.job = Some("1234abcd".into());
        assert!(!end_moved(home.path(), &holders, &claude), "a job is stopped by its id");
    }

    #[tokio::test]
    async fn local_sessions_the_owners_own_end_stops_a_session_that_is_not_shared_and_no_other_command_does() {
        fn unchanged(_: PathBuf, native: NativeSession) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<NativeSession>> + Send>> {
            Box::pin(async move { Ok(native) })
        }
        let dir = tempfile::tempdir().unwrap();
        let mut child = std::process::Command::new("sleep").arg("60").spawn().unwrap();
        let mut mirror = test_mirror("5eed00e0-0000-4000-8000-0000000000e0", "g1");
        mirror.session_id = Some(SID.into());
        mirror.shared = false; // Only me
        mirror.native.provider = "claude".into();
        mirror.native.process = process(child.id()).unwrap();
        // Claude's record of this pid names the conversation, as a live session's does.
        let conversation = mirror.native.conversation.clone();
        let sessions = dir.path().join(".claude/sessions");
        fs::create_dir_all(&sessions).unwrap();
        let pid = child.id();
        let record = |conversation: &str| fs::write(sessions.join(format!("{pid}.json")), json!({"pid": pid, "sessionId": conversation}).to_string()).unwrap();
        let mut host = test_host(dir.path(), vec![("own-end", mirror)]);
        host.verify = unchanged;
        let at = epoch_secs();
        let anchor = ClockAnchor { server: at, round_trip: 0.0, at: Instant::now(), wall: SystemTime::now() };
        let command = |request: &str, kind: &str, turn: Value, seq: u64| parse_command(&json!({"command":{"requestId":request,
            "expectedGeneration":"g1","kind":kind,"text":null,"expectedTurnId":turn,"sessionId":SID,"roomId":"","actorUserId":"alice",
            "commandSeq":seq,"state":"dispatched","createdAt":at,"deliverBy":at + 30,"held":false,"providerMessageId":null}}), "alice").unwrap();
        let refused = host.command(&command("r1", "interrupt", json!("turn-1"), 1), Some(&anchor)).await.unwrap();
        assert_eq!(refused.outcome, "rejected", "their own, and still refused: only End is carried out");
        assert!(child.try_wait().unwrap().is_none());
        // Same pid, but Claude switched it to another conversation (`/resume`, `/clear`) before the
        // next liveness tick: End is refused, never sent to the conversation that moved in.
        record("99999999-9999-4999-8999-999999999999");
        let moved = host.command(&command("r1b", "end", Value::Null, 2), Some(&anchor)).await.unwrap();
        assert_eq!(moved.outcome, "rejected");
        assert!(child.try_wait().unwrap().is_none(), "no signal reached the process");
        record(&conversation);
        let ended = host.command(&command("r2", "end", Value::Null, 3), Some(&anchor)).await.unwrap();
        assert_eq!(ended.outcome, "applied");
        assert!(!child.wait().unwrap().success(), "SIGTERM reached its process");
        assert!(!host.mirrors["own-end"].shared, "ending it shared nothing");

        // A Claude background session (← moved it to Claude's daemon, which keeps it running when
        // its person's terminal is gone): ended by Claude's own `stop`, never by a signal, and
        // the end that follows is one its person asked for.
        use std::os::unix::fs::PermissionsExt;
        const JOB: &str = "0b0b0b0b-0000-4000-8000-0000000000b0";
        let bin = dir.path().join(".local/bin");
        fs::create_dir_all(&bin).unwrap();
        fs::write(bin.join("claude"), "#!/bin/sh\necho \"$@\" > \"$0.args\"\n").unwrap();
        fs::set_permissions(bin.join("claude"), fs::Permissions::from_mode(0o755)).unwrap();
        let mut child = std::process::Command::new("sleep").arg("60").spawn().unwrap();
        let mut job = test_mirror("5eed00e1-0000-4000-8000-0000000000e1", "g1");
        job.session_id = Some(JOB.into());
        job.native.provider = "claude".into();
        job.native.job = Some("1234abcd".into());
        job.native.process = process(child.id()).unwrap();
        host.mirrors.insert("job-end".into(), job);
        let mut end = command("r3", "end", Value::Null, 1);
        end.session_id = JOB.into();
        assert!(crate::holder::manager::end_asked(JOB).is_none());
        assert_eq!(host.command(&end, Some(&anchor)).await.unwrap().outcome, "applied");
        assert_eq!(fs::read_to_string(bin.join("claude.args")).unwrap().trim(), "stop 1234abcd");
        assert!(crate::holder::manager::end_asked(JOB).is_some());
        assert!(child.try_wait().unwrap().is_none(), "no signal: Claude's daemon stops its own session");

        // Refused, with no stop, no signal and no ask recorded: a teammate's End, an id that is
        // not a job id, and a stop Claude did not carry out (not applied: it runs on).
        crate::holder::manager::end_asked_forget(JOB);
        fs::remove_file(bin.join("claude.args")).unwrap();
        let mut theirs = command("r4", "end", Value::Null, 2);
        theirs.session_id = JOB.into();
        theirs.own = false;
        assert_eq!(host.command(&theirs, Some(&anchor)).await.unwrap().outcome, "rejected");
        host.mirrors.get_mut("job-end").unwrap().native.job = Some("../../x".into());
        let mut forged = command("r5", "end", Value::Null, 3);
        forged.session_id = JOB.into();
        assert_eq!(host.command(&forged, Some(&anchor)).await.unwrap().outcome, "rejected");
        assert!(!bin.join("claude.args").exists() && crate::holder::manager::end_asked(JOB).is_none());
        host.mirrors.get_mut("job-end").unwrap().native.job = Some("1234abcd".into());
        fs::write(bin.join("claude"), "#!/bin/sh\nexit 1\n").unwrap();
        let mut failed = command("r6", "end", Value::Null, 4);
        failed.session_id = JOB.into();
        assert_ne!(host.command(&failed, Some(&anchor)).await.unwrap().outcome, "applied");
        assert!(crate::holder::manager::end_asked(JOB).is_none(), "a later crash is not an end its person asked for");
        assert!(child.try_wait().unwrap().is_none());

        // A job a holder of ours shows (`claude attach`, the page watching it): Claude's daemon
        // still stops the job. Ending only that client left the job running with no row.
        fs::write(bin.join("claude"), "#!/bin/sh\necho \"$@\" > \"$0.args\"\n").unwrap();
        host.mirrors.get_mut("job-end").unwrap().native.holder = Some("a77ac4ed".into());
        let mut shown = command("r7", "end", Value::Null, 5);
        shown.session_id = JOB.into();
        assert_eq!(host.command(&shown, Some(&anchor)).await.unwrap().outcome, "applied");
        assert_eq!(fs::read_to_string(bin.join("claude.args")).unwrap().trim(), "stop 1234abcd");
        assert!(crate::holder::manager::end_asked("a77ac4ed").is_some(), "the end that follows is one its person asked for");
        crate::holder::manager::end_asked_forget("a77ac4ed");
        child.kill().unwrap();
        child.wait().unwrap();
    }

    #[test]
    fn local_sessions_a_withheld_session_holds_no_prompt_for_a_remote_answer_and_ends_without_draining() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror("5eed0005-0000-4000-8000-000000000005", "gen-1");
        mirror.shared = false;
        mirror.queue.push(json!({"kind":"text"}));
        let mut host = test_host(dir.path(), vec![("k", mirror)]);
        let event = lifecycle::HookEvent {
            provider: lifecycle::Provider::Codex,
            source_process: Some(lifecycle::HookProcess::of(&process(std::process::id()).unwrap())),
            event: "PermissionRequest".into(),
            session_id: "5eed0005-0000-4000-8000-000000000005".into(),
            transcript_path: None,
            cwd: None,
            notification_type: None,
            message: None,
            reason: None,
            last_assistant_message: None,
            tool_name: Some("Bash".into()),
            tool_use_id: Some("tool-1".into()),
            waiting_text: Some("rm -rf build".into()),
            held: None,
        };
        host.apply_hook(&event).unwrap();
        let mirror = &host.mirrors["k"];
        assert_eq!(mirror.native.status, "waiting-approval", "its own status still moves");
        assert_eq!(mirror.queue.len(), 1, "no approval row waits to be uploaded");
        // Its end waits for nothing it will never send.
        host.mirrors.get_mut("k").unwrap().deferred_end_reason = Some("exited".into());
        host.finish_deferred_end("k").unwrap();
        assert!(host.mirrors["k"].ended_at.is_some());
        assert!(!has_drainable_backlog(&host.mirrors["k"]));
    }

    #[test]
    fn local_sessions_a_claude_session_never_shared_registers_nothing_when_it_comes_back_and_ends() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![]);
        host.share_every = Some(false);
        let conversation = "5eed0006-0000-4000-8000-000000000006";
        let mut native = test_native(conversation);
        native.provider = "claude".into();
        // Claude's own registry: this pid runs the conversation (again).
        fs::create_dir_all(dir.path().join(".claude/sessions")).unwrap();
        fs::write(dir.path().join(format!(".claude/sessions/{}.json", std::process::id())), format!("{{\"sessionId\":\"{conversation}\"}}")).unwrap();
        host.observe(vec![native.clone()]).unwrap();
        let key = native_key(&native);
        assert!(!host.mirrors[&key].shared);
        // Its person opens the agent view (it ends here), comes back (it is revived), then exits.
        host.end_session(&key, "exited").unwrap();
        host.observe(vec![native.clone()]).unwrap();
        assert!(host.mirrors[&key].ended_at.is_none() && host.mirrors[&key].resume_registration_pending, "revived as a resume");
        host.end_session(&key, "exited").unwrap();
        let mirror = &host.mirrors[&key];
        assert!(mirror.ended_at.is_some() && mirror.deferred_end_reason.is_none(), "its end waits for no register");
        assert_eq!(registration_due_with_recovery(&host.mirrors, &BTreeSet::new(), &BTreeMap::new(), &BTreeSet::new()), None);
        // The relay never had it: no end is told.
        assert!(host.ended_frame(&mut BTreeSet::new()).unwrap().is_none());
        assert!(host.mirrors[&key].ended_sent && host.ended_pending.is_empty());

        // One the relay has had (shared, registered, then stopped) and whose first end was never
        // told: it comes back and exits. The end is told, under the generation the relay knows.
        let known = host.mirrors[&key].generation.clone();
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.session_id = Some("5eed0006-5e55-4000-8000-000000000006".into());
        mirror.registration.acknowledged = Some(known.clone());
        mirror.ended_sent = false;
        // Its person had asked the run before to end (End): that says nothing about this one's end,
        // a new process. (On the same process an End asked keeps it ended.)
        crate::holder::manager::end_asked_now("5eed0006-5e55-4000-8000-000000000006");
        let mut native = native.clone();
        native.process.birth_micros += 1;
        host.observe(vec![native.clone()]).unwrap();
        assert_ne!(host.mirrors[&key].generation, known, "revived under a generation that never registers");
        assert!(crate::holder::manager::end_asked("5eed0006-5e55-4000-8000-000000000006").is_none(), "its next end is a plain exit");
        host.end_session(&key, "exited").unwrap();
        let (_, frame) = host.ended_frame(&mut BTreeSet::new()).unwrap().expect("its end is told");
        assert_eq!((frame["type"].as_str(), frame["generation"].as_str()), (Some("local.session.ended"), Some(known.as_str())));
    }

    #[test]
    fn local_sessions_the_workspace_s_pause_and_the_band_s_stop_are_one_switch() {
        let dir = tempfile::tempdir().unwrap();
        let sid = "5eed000a-5e55-4000-8000-00000000000a";
        let mut mirror = test_mirror("5eed000a-0000-4000-8000-00000000000a", "gen-1");
        mirror.session_id = Some(sid.into());
        let key = "5eed000a".repeat(8);
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        let presence = |paused: bool, generation: &str| json!({"sessionId": sid, "generation": generation, "paused": paused});
        // Paused in the workspace (the web's switch): the Mac stops sending. Another run's presence says nothing.
        assert_eq!(host.follow_presence(&presence(true, "gen-0")), None);
        assert!(host.mirrors[&key].shared);
        assert_eq!(host.follow_presence(&presence(true, "gen-1")), None);
        assert!(host.mirrors[&key].withheld() && !host.mirrors[&key].stop_owed);
        assert!(!private_read::<Mirror>(&dir.path().join(format!("{key}.json"))).unwrap().shared, "durable");
        // Shared again there: shared here, and it registers again.
        assert_eq!(host.follow_presence(&presence(false, "gen-1")), Some(key.clone()));
        assert!(host.mirrors[&key].shared);

        // Stopped at the band: nothing is sent at once, and the stop is owed to the workspace
        // until it takes it. Its presence still says "shared" meanwhile: that undoes nothing.
        ask_share(&key, false);
        assert!(host.stopped_now(&key), "before the loop applies it");
        assert!(host.refuses_frame(&json!({"sessionId": sid})), "a terminal frame already queued is not sent");
        assert!(host.share_tick().is_empty());
        assert!(host.mirrors[&key].withheld() && host.mirrors[&key].stop_owed);
        assert_eq!(host.follow_presence(&presence(false, "gen-1")), None);
        assert!(!host.mirrors[&key].shared);
        // The workspace took it: nothing is owed, and a "shared" read just before the pause is stale.
        stop_settled(&key);
        assert!(host.share_tick().is_empty());
        assert!(!host.mirrors[&key].stop_owed && !is_settled(&key));
        assert_eq!(host.follow_presence(&presence(false, "gen-1")), None);
        assert!(!host.mirrors[&key].shared);
    }

    #[test]
    fn local_sessions_a_shared_plain_claude_registers_and_uploads_its_conversation() {
        // Owner ruling 2026-10-11: a session with no live terminal is still shared, as its conversation.
        let mut mirror = test_mirror("5eed000c-0000-4000-8000-00000000000c", "gen-1");
        mirror.native.provider = "claude".into();
        mirror.native.app = false;
        assert_eq!(mirror.native.terminal_reason(), Some("plain"));
        mirror.queue.push(json!({"kind":"text"}));
        let mut mirrors = BTreeMap::from([("plain".to_string(), mirror)]);
        let due = |mirrors: &BTreeMap<String, Mirror>| registration_due_with_recovery(mirrors, &BTreeSet::new(), &BTreeMap::new(), &BTreeSet::new());
        assert_eq!(due(&mirrors), Some("plain".to_string()), "it registers");
        mirrors.get_mut("plain").unwrap().session_id = Some("5eed000c-5e55-4000-8000-00000000000c".into()); // registered
        assert!(mirrors.get_mut("plain").unwrap().next_batch().unwrap().is_some(), "and uploads");
        // Only its person's: nothing leaves this Mac.
        let mirror = mirrors.get_mut("plain").unwrap();
        mirror.shared = false;
        mirror.pending = None;
        assert!(mirror.next_batch().unwrap().is_none());
        // It still registers, as Only you, so its owner keeps it in their list.
        assert_eq!(due(&mirrors), Some("plain".to_string()));
        assert_eq!(registration(&mirrors["plain"])["session"]["shared"], false);
    }

    #[test]
    fn local_sessions_a_codex_off_its_daemon_offers_no_send() {
        // It is refused on the Mac (`command`), so the relay is told it has no input connection:
        // the backend then grants it no send mode and the clients offer no composer.
        let mut mirror = test_mirror("5eed000e-0000-4000-8000-00000000000e", "gen-1");
        mirror.session_id = Some(SID.into());
        mirror.native.app = false;
        mirror.native.process.executable = "/opt/homebrew/bin/codex".into();
        assert_eq!(mirror.native.terminal_reason(), Some("no-daemon"));
        assert_eq!(registration(&mirror)["session"]["controlUnavailableReason"], "endpoint-unavailable");
        let key = native_key(&mirror.native);
        let beats = |m: &Mirror| device_heartbeats(&BTreeMap::from([(key.clone(), m.clone())]), &BTreeSet::from([key.clone()]), false).unwrap();
        assert_eq!(beats(&mirror)[0]["sessions"][0]["controlUnavailableReason"], "endpoint-unavailable");
        // A plain `claude` takes sends through its inbox socket: nothing withdrawn.
        mirror.native.provider = "claude".into();
        assert_eq!(registration(&mirror)["session"]["controlUnavailableReason"], Value::Null);
        assert_eq!(beats(&mirror)[0]["sessions"][0]["controlUnavailableReason"], Value::Null);
        // Its own restriction still wins.
        mirror.native.provider = "codex".into();
        mirror.native.restriction = Some("history-gap".into());
        assert_eq!(registration(&mirror)["session"]["controlUnavailableReason"], "history-gap");
    }

    #[test]
    fn local_sessions_a_registered_session_resumed_in_a_plain_terminal_is_shared_and_ends_under_its_own_generation() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![]);
        let mut native = test_native("5eed0009-0000-4000-8000-000000000009");
        native.app = false;
        native.holder = Some("ab12cd34".into());
        host.observe(vec![native.clone()]).unwrap();
        let key = native_key(&native);
        // Shared and registered at G1; its end is never told (the Mac was offline).
        let known = host.mirrors[&key].generation.clone();
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.session_id = Some("5eed0009-5e55-4000-8000-000000000009".into());
        mirror.registration.acknowledged = Some(known.clone());
        host.end_session(&key, "exited").unwrap();
        // Resumed with plain `codex`: a new process, no holder. Shared as its conversation now
        // (owner ruling 2026-10-11), so it is a resume like any other: its run registers.
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let mut resumed = native.clone();
        resumed.process = process(child.id()).unwrap();
        resumed.holder = None;
        resumed.status = "busy".into();
        let observed = host.observe(vec![resumed]);
        child.kill().unwrap();
        child.wait().unwrap();
        observed.unwrap();
        let mirror = &host.mirrors[&key];
        let current = mirror.generation.clone();
        assert!(mirror.ended_at.is_none() && !mirror.withheld() && current != known && !mirror.resume_registration_pending, "the successor path");
        assert_eq!(registration_due(&host.mirrors, &BTreeSet::new(), &BTreeMap::new()), Some(key.clone()), "its run registers");
        // Then it exits: its end is told under its own generation, not rewound to the one before.
        host.end_session(&key, "exited").unwrap();
        let (_, frame) = host.ended_frame(&mut BTreeSet::new()).unwrap().expect("its end is told");
        assert_eq!(frame["generation"].as_str(), Some(current.as_str()));
        assert_eq!(frame["reason"], "exited");
    }

    #[tokio::test]
    async fn local_sessions_a_send_to_a_codex_off_its_daemon_is_refused_not_queued_on_the_daemon() {
        fn unchanged(_: PathBuf, native: NativeSession) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<NativeSession>> + Send>> {
            Box::pin(async move { Ok(native) })
        }
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        // A `codex` that records any call: `codex queue` would hand the thread to the shared daemon,
        // a second writer beside the TUI that owns it.
        let bin = dir.path().join("codex");
        fs::write(&bin, "#!/bin/sh\necho \"$@\" > \"$0.args\"\n").unwrap();
        fs::set_permissions(&bin, fs::Permissions::from_mode(0o755)).unwrap();
        let mut mirror = test_mirror("5eed000d-0000-4000-8000-00000000000d", "g1");
        mirror.session_id = Some(SID.into());
        mirror.native.app = false;
        mirror.native.process.executable = bin.clone();
        assert_eq!(mirror.native.terminal_reason(), Some("no-daemon"));
        let mut host = test_host(dir.path(), vec![("no-daemon", mirror)]);
        host.verify = unchanged;
        let at = epoch_secs();
        let anchor = ClockAnchor { server: at, round_trip: 0.0, at: Instant::now(), wall: SystemTime::now() };
        let send = parse_command(&json!({"command":{"requestId":"r1","expectedGeneration":"g1","kind":"send","text":"hello",
            "expectedTurnId":null,"sessionId":SID,"roomId":"","actorUserId":"alice","commandSeq":1,"state":"dispatched",
            "createdAt":at,"deliverBy":at + 30,"held":false,"providerMessageId":null}}), "alice").unwrap();
        assert_eq!(host.command(&send, Some(&anchor)).await.unwrap().outcome, "rejected");
        assert!(!dir.path().join("codex.args").exists(), "nothing was queued");
    }

    #[tokio::test]
    async fn local_sessions_a_send_to_a_held_codex_is_queued_although_verification_drops_the_holder() {
        // Verification recaptures the session, and a capture never names a holder.
        fn recaptured(_: PathBuf, native: NativeSession) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<NativeSession>> + Send>> {
            Box::pin(async move { Ok(NativeSession { holder: None, ..native }) })
        }
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("codex");
        fs::write(&bin, "#!/bin/sh\necho \"$@\" > \"$0.args\"\n").unwrap();
        fs::set_permissions(&bin, fs::Permissions::from_mode(0o755)).unwrap();
        let mut mirror = test_mirror("5eed000f-0000-4000-8000-00000000000f", "g1");
        mirror.session_id = Some(SID.into());
        mirror.native.app = false;
        mirror.native.process.executable = bin.clone();
        mirror.native.holder = Some("01648c21".into()); // `lightbulb codex`: on the daemon, in a holder
        let rollout = dir.path().join("rollout.jsonl"); // the send anchors at the rollout's end
        fs::write(&rollout, "{}\n").unwrap();
        mirror.native.transcript = Some(rollout);
        assert_eq!(mirror.native.terminal_reason(), None);
        let mut host = test_host(dir.path(), vec![("held", mirror)]);
        host.verify = recaptured;
        let at = epoch_secs();
        let anchor = ClockAnchor { server: at, round_trip: 0.0, at: Instant::now(), wall: SystemTime::now() };
        let send = parse_command(&json!({"command":{"requestId":"r1","expectedGeneration":"g1","kind":"send","text":"hello",
            "expectedTurnId":null,"sessionId":SID,"roomId":"","actorUserId":"alice","commandSeq":1,"state":"dispatched",
            "createdAt":at,"deliverBy":at + 30,"held":false,"providerMessageId":null}}), "alice").unwrap();
        let _ = host.command(&send, Some(&anchor)).await;
        let args = fs::read_to_string(dir.path().join("codex.args")).expect("the send was queued on the daemon");
        assert!(args.starts_with("queue --thread"), "{args}");
    }

    #[test]
    fn local_sessions_a_shared_generation_drains_before_its_plain_successor_takes_over() {
        let mut mirror = test_mirror("5eed000b-0000-4000-8000-00000000000b", "gen-1");
        mirror.native.app = false;
        mirror.native.holder = Some("ab12cd34".into());
        mirror.session_id = Some("5eed000b-5e55-4000-8000-00000000000b".into());
        mirror.queue.push(json!({"kind":"text"}));
        // The conversation restarts in a plain terminal while the run before still has history to send.
        let mut plain = mirror.native.clone();
        plain.holder = None;
        mirror.registration.successor = Some(plain);
        assert!(!mirror.advance(), "the run before drains first");
        assert!(!mirror.withheld());
        assert!(mirror.next_batch().unwrap().is_some());
        mirror.queue.clear();
        mirror.pending = None;
        assert!(mirror.advance());
        assert!(!mirror.withheld(), "promoted: a plain terminal shares its conversation (owner ruling 2026-10-11)");
    }

    fn falsely_ended_codex_terminal(dir: &Path) -> (Host, String, NativeSession) {
        let mut native = test_native("5eed0010-0000-4000-8000-000000000010");
        native.app = false;
        native.status = "idle".into();
        native.holder = Some("ab12cd34".into());
        let mut host = test_host(dir, vec![]);
        host.observe(vec![native.clone()]).unwrap();
        let key = native_key(&native);
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.session_id = Some(SID.into());
        mirror.registered();
        host.end_session(&key, "exited").unwrap();
        (host, key, native)
    }

    #[test]
    fn local_sessions_a_live_codex_terminal_repairs_its_same_process_false_end_without_a_prompt() {
        let dir = tempfile::tempdir().unwrap();
        let (mut host, key, native) = falsely_ended_codex_terminal(dir.path());
        let old = host.mirrors[&key].generation.clone();
        host.observe(vec![native.clone()]).unwrap();
        let mirror = &host.mirrors[&key];
        assert!(mirror.ended_at.is_none(), "a verified idle terminal is still open");
        assert_eq!(mirror.native.status, "idle");
        assert_eq!(mirror.native.process, native.process, "no second writer or new prompt needed");
        assert_ne!(mirror.generation, old, "fence the ended run's receipts");
        assert_eq!(mirror.previous_generation.as_deref(), Some(old.as_str()));
        assert_eq!(registration_due_with_recovery(&host.mirrors, &BTreeSet::new(), &BTreeMap::new(), &BTreeSet::new()), Some(key.clone()));
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.registered();
        mirror.pending = None;
        mirror.queue.clear();
        host.finish_deferred_end(&key).unwrap();
        assert!(host.mirrors[&key].ended_at.is_none(), "registration and history drain leave the terminal usable");
    }

    #[test]
    fn local_sessions_codex_terminal_recovery_requires_a_fresh_live_unintended_end() {
        for case in ["no-holder", "ended-native", "intentional", "stale-discovery"] {
            let dir = tempfile::tempdir().unwrap();
            let (mut host, key, mut native) = falsely_ended_codex_terminal(dir.path());
            match case {
                "no-holder" => native.holder = None,
                "ended-native" => native.status = "ended".into(),
                "intentional" => host.mirrors.get_mut(&key).unwrap().ended_reason = Some(super::super::lifecycle::ON_PURPOSE.into()),
                "stale-discovery" => {
                    host.mirrors.get_mut(&key).unwrap().ended_at_ms = Some(5000);
                    host.discovery_read_ms = Some(4999);
                }
                _ => unreachable!(),
            }
            host.observe(vec![native]).unwrap();
            assert!(host.mirrors[&key].ended_at.is_some(), "{case} cannot revive the run");
        }
    }

    #[test]
    fn local_sessions_a_shared_codex_thread_resumed_under_a_holder_while_idle_revives_and_registers() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![]); // "Share every new session" is on
        let mut native = test_native("5eed0008-0000-4000-8000-000000000008");
        native.app = false; // a plain terminal: shared as its conversation
        native.status = "idle".into();
        host.observe(vec![native.clone()]).unwrap();
        let key = native_key(&native);
        assert!(host.mirrors[&key].shared && !host.mirrors[&key].withheld());
        host.end_session(&key, "exited").unwrap();
        // Resumed (or picked up) with Lightbulb: a new process under a holder, idle when seen.
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let mut resumed = native.clone();
        resumed.process = process(child.id()).unwrap();
        resumed.holder = Some("ab12cd34".into());
        let observed = host.observe(vec![resumed]);
        child.kill().unwrap();
        child.wait().unwrap();
        observed.unwrap();
        assert!(host.mirrors[&key].ended_at.is_none() && !host.mirrors[&key].withheld(), "judged by the run that is live now");
        assert!(host.mirrors[&key].deferred_end_reason.is_none(),
            "an idle held terminal waits for input; draining history must not end it");
        assert_eq!(registration_due_with_recovery(&host.mirrors, &BTreeSet::new(), &BTreeMap::new(), &BTreeSet::new()), Some(key.clone()));
        // The register and history upload finish before the person enters their first prompt.
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.registered();
        mirror.pending = None;
        mirror.queue.clear();
        host.finish_deferred_end(&key).unwrap();
        assert!(host.mirrors[&key].ended_at.is_none(), "history drain keeps the input-ready terminal live");
        assert_eq!(host.mirrors[&key].native.status, "idle");
    }

    #[test]
    fn local_sessions_a_codex_thread_never_shared_registers_nothing_when_it_is_resumed_idle() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![]);
        host.share_every = Some(false);
        let mut native = test_native("5eed0007-0000-4000-8000-000000000007");
        native.status = "idle".into();
        host.observe(vec![native.clone()]).unwrap();
        let key = native_key(&native);
        host.end_session(&key, "exited").unwrap();
        // `codex resume`: the same thread in a new process, idle when discovery sees it.
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let mut resumed = native.clone();
        resumed.process = process(child.id()).unwrap();
        let observed = host.observe(vec![resumed]);
        child.kill().unwrap();
        child.wait().unwrap();
        observed.unwrap();
        let mirror = &host.mirrors[&key];
        assert!(mirror.ended_at.is_some() && mirror.deferred_end_reason.is_none(), "no end deferred behind a register");
        assert_eq!(registration_due_with_recovery(&host.mirrors, &BTreeSet::new(), &BTreeMap::new(), &BTreeSet::new()), None);
    }

    #[test]
    fn local_sessions_heartbeat_names_the_terminal_only_to_a_relay_that_reads_it() {
        let mut mirror = test_mirror("thread-1", "generation-1");
        mirror.session_id = Some(SID.into());
        let key = native_key(&mirror.native);
        let registered = BTreeSet::from([key.clone()]);
        let entry = |m: &Mirror, reads: bool| {
            device_heartbeats_with_controls(&BTreeMap::from([(key.clone(), m.clone())]), &registered, false, false, false, false, reads, false, false, false, false)
                .unwrap()[0]["sessions"][0].clone()
        };
        assert!(entry(&mirror, false).get("terminal").is_none(), "an older relay rejects the whole frame over it");
        assert_eq!(entry(&mirror, true)["terminal"], "none");
        mirror.native.job = Some("8ed71738".into());
        assert_eq!(entry(&mirror, true)["terminal"], "attach");
        mirror.native.holder = Some("ab12cd34".into());
        assert_eq!(entry(&mirror, true)["terminal"], "held");
    }

    #[test]
    fn local_sessions_heartbeat_advertises_pickup_requests_only_to_a_relay_that_lists_them() {
        let mut mirror = test_mirror("thread-1", "generation-1");
        mirror.session_id = Some(SID.into());
        let key = native_key(&mirror.native);
        let frame = |reads: bool| {
            device_heartbeats_with_controls(&BTreeMap::from([(key.clone(), mirror.clone())]), &BTreeSet::from([key.clone()]), false, false, false, false, false, true, reads, false, false)
                .unwrap()[0]
                .clone()
        };
        assert!(frame(false).get("pickupRequests").is_none(), "an older relay rejects the whole frame over it");
        assert_eq!(frame(true)["pickupRequests"], true);
        assert_eq!(frame(true)["sessionPresence"], true);
    }

    #[test]
    fn local_sessions_heartbeat_advertises_band_owner_only_only_to_a_relay_that_lists_it() {
        let mut mirror = test_mirror("thread-1", "generation-1");
        mirror.session_id = Some(SID.into());
        let key = native_key(&mirror.native);
        let frame = |reads: bool| {
            device_heartbeats_with_controls(&BTreeMap::from([(key.clone(), mirror.clone())]), &BTreeSet::from([key.clone()]), false, false, false, false, false, true, true, reads, false)
                .unwrap()[0]
                .clone()
        };
        assert!(frame(false).get("bandOwnerOnly").is_none(), "an older relay rejects the whole frame over it");
        assert_eq!(frame(true)["bandOwnerOnly"], true);
    }

    #[test]
    fn local_sessions_a_fork_registers_with_the_session_it_continues() {
        let mut origin = test_mirror("11111111-1111-4111-8111-111111111111", "gen-1");
        origin.native.provider = "claude".into();
        origin.session_id = Some(SID.into());
        let mut fork = test_mirror("22222222-2222-4222-8222-222222222222", "gen-2");
        fork.native.provider = "claude".into();
        fork.native.forked_from = Some(origin.native.conversation.clone());
        let mirrors = BTreeMap::from([(native_key(&origin.native), origin.clone()), (native_key(&fork.native), fork.clone())]);
        assert_eq!(origin_session(&mirrors, &fork.native).as_deref(), Some(SID));
        assert_eq!(origin_session(&mirrors, &origin.native), None);
        fork.native.provider = "codex".into();
        assert_eq!(origin_session(&mirrors, &fork.native), None, "only the same provider and profile");
    }

    #[test]
    fn local_sessions_a_fork_registered_before_its_origin_id_registers_again_with_it() {
        let dir = tempfile::tempdir().unwrap();
        let mut origin = test_mirror(SID, "gen-1");
        origin.native.provider = "claude".into();
        let mut fork = test_mirror(SID_B, "gen-2");
        fork.native.provider = "claude".into();
        fork.native.forked_from = Some(SID.into());
        let (origin_key, fork_key) = (native_key(&origin.native), native_key(&fork.native));
        let mut host = test_host(dir.path(), vec![(origin_key.as_str(), origin), (fork_key.as_str(), fork)]);
        // The fork registers first: its origin has no Lightbulb id yet.
        assert!(host.begin_registration(&fork_key).unwrap()["session"].get("forkedFrom").is_none());
        // The origin's ACK arrives; the fork (and only the fork) is queued once more.
        host.mirrors.get_mut(&origin_key).unwrap().session_id = Some("origin-session".into());
        assert_eq!(forks_of(&host.mirrors, &origin_key), vec![fork_key.clone()]);
        assert!(forks_of(&host.mirrors, &fork_key).is_empty());
        assert_eq!(host.begin_registration(&fork_key).unwrap()["session"]["forkedFrom"], "origin-session");
    }

    #[test]
    fn local_sessions_lineage_found_late_reregisters_the_fork() {
        let dir = tempfile::tempdir().unwrap();
        let mut fork = test_mirror(SID_B, "gen-2");
        fork.native.provider = "claude".into();
        let key = native_key(&fork.native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), fork.clone())]);
        let mut native = fork.native.clone();
        native.forked_from = Some(SID.into());
        assert_eq!(host.observe(vec![native]).unwrap(), vec![key]);
    }

    #[test]
    fn local_sessions_a_codex_thread_discovery_stopped_naming_reads_its_rollout() {
        let dir = tempfile::tempdir().unwrap();
        let rollout = dir.path().join("rollout.jsonl");
        fs::write(&rollout, "{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"t1\"}}\n{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\",\"turn_id\":\"t1\"}}\n").unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.transcript = Some(rollout);
        let key = native_key(&mirror.native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        // Its app server (this test's pid) lives on but no longer holds the thread open.
        host.observe(vec![]).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "idle");
        assert!(host.heartbeat_dirty.contains(&key), "the relay hears it on the next heartbeat");
        host.mirrors.get_mut(&key).unwrap().native.status = "waiting-approval".into();
        host.observe(vec![]).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "waiting-approval", "a hook's open prompt is kept");
    }

    #[test]
    fn local_sessions_a_live_session_no_listing_names_is_idle_not_unknown_after_a_restart() {
        let dir = tempfile::tempdir().unwrap();
        // A Codex thread whose rollout is gone, restored as "unknown": no hook fires for an idle
        // session. A Claude session's "unknown" is the engine's to clear.
        let mut claude = test_mirror(SID, "gen-1");
        claude.native.provider = "claude".into();
        claude.native.restriction = Some("runtime-unverified".into());
        let mut codex = test_mirror(SID_B, "gen-1");
        codex.native.restriction = Some("history-gap".into());
        codex.native.transcript = Some(dir.path().join("archived-rollout.jsonl"));
        let mut gone = test_mirror("cccc0000-0000-4000-8000-000000000000", "gen-1");
        gone.native.process.pid = 2_147_483_000;
        let keys: Vec<String> = [&claude, &codex, &gone].iter().map(|m| native_key(&m.native)).collect();
        let mut host = test_host(
            dir.path(),
            vec![(keys[0].as_str(), claude), (keys[1].as_str(), codex), (keys[2].as_str(), gone)],
        );
        host.observe(vec![]).unwrap();
        assert_eq!(host.mirrors[&keys[0]].native.status, "unknown");
        assert_eq!(host.mirrors[&keys[1]].native.status, "idle");
        assert_ne!(host.mirrors[&keys[2]].native.status, "idle", "a dead process is not online");
        assert_eq!(
            host.mirrors[&keys[1]].native.restriction.as_deref(),
            Some("history-gap"),
            "online is a status, not verified control"
        );
        // A prompt whose remote hold was dropped is unresolved: the terminal may still be
        // blocked on it, so it is not called idle.
        let mut asking = test_mirror("dddd0000-0000-4000-8000-000000000000", "gen-1");
        asking.waiting = Some(WaitingPrompt { summary: "Run tests?".into(), ..Default::default() });
        let key = native_key(&asking.native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), asking)]);
        host.observe(vec![]).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "unknown");
    }

    #[test]
    fn local_sessions_a_resumed_session_shows_its_successor_status_while_history_drains() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.provider = "claude".into();
        mirror.session_id = Some("session".into());
        // The old generation's batch is still unacknowledged, so `advance` cannot promote yet.
        mirror.queue = vec![json!({"kind":"text","role":"assistant","text":"old","truncated":false})];
        mirror.pending = Some(json!({"batchId":"old","afterSeq":0,"events":[]}));
        let key = native_key(&mirror.native);
        let mut resumed = mirror.native.clone();
        resumed.process.birth_micros += 1;
        resumed.status = "busy".into();
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        host.observe(vec![resumed]).unwrap();
        let mirror = &host.mirrors[&key];
        assert!(mirror.registration.successor.is_some(), "not promoted before the drain");
        assert_eq!(mirror.native.status, "busy", "the running process's status, not unknown");
        assert_eq!(mirror.native.restriction.as_deref(), Some("runtime-unverified"));
    }

    #[test]
    fn local_sessions_a_fork_keeps_its_lineage_through_a_failed_ps() {
        let dir = tempfile::tempdir().unwrap();
        let mut fork = test_mirror(SID_B, "gen-1");
        fork.native.provider = "claude".into();
        fork.native.forked_from = Some(SID.into());
        let key = native_key(&fork.native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), fork.clone())]);
        let mut again = fork.native.clone();
        again.forked_from = None;
        host.observe(vec![again]).unwrap();
        assert_eq!(host.mirrors[&key].native.forked_from.as_deref(), Some(SID));
    }

    #[test]
    fn local_sessions_a_fork_ends_the_session_it_was_moved_out_of() {
        let dir = tempfile::tempdir().unwrap();
        // Its pid is this test process: alive, as the original's process stays after ←.
        let mut origin = test_mirror(SID, "gen-1");
        origin.native.provider = "claude".into();
        origin.session_id = Some("origin-session".into());
        let key = native_key(&origin.native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), origin.clone())]);
        let mut fork = test_native(SID_B);
        fork.provider = "claude".into();
        fork.forked_from = Some(SID.into());
        host.observe(vec![origin.native.clone(), fork.clone()]).unwrap();
        assert!(host.mirrors[&key].ended_at.is_none(), "an original still listed beside its fork keeps running");
        host.observe(vec![fork.clone()]).unwrap();
        assert!(host.mirrors[&key].ended_at.is_none(), "discovery ends nothing: the engine says");
        let mut fork_row = engine_row("live");
        fork_row.conversation = SID_B.into();
        let moved_on = Ok(EngineView { live: vec![fork_row], ended: vec![engine_ended(Some("moved-on"))] });
        host.apply_engine(&moved_on);
        host.apply_engine(&moved_on);
        assert_eq!(host.mirrors[&key].ended_reason.as_deref(), Some("exited"), "the engine's moved-on ends the original");
        assert_eq!(host.ended_pending, vec![key.clone()]);
        host.observe(vec![fork]).unwrap();
        assert_eq!(host.ended_pending, vec![key], "ended once");
        let fork_key = native_key(&host.mirrors.values().find(|m| m.native.conversation == SID_B).unwrap().native);
        assert_eq!(
            host.begin_registration(&fork_key).unwrap()["session"]["forkedFrom"],
            "origin-session",
            "the fork still names the original it continues"
        );
    }

    async fn next_frame(socket: &mut WebSocketStream<tokio::net::TcpStream>) -> Value {
        let frame = tokio::time::timeout(Duration::from_secs(2), socket.next())
            .await
            .expect("frame within 2s")
            .expect("stream open")
            .expect("frame ok");
        serde_json::from_str(frame.to_text().unwrap()).unwrap()
    }

    async fn reply(socket: &mut WebSocketStream<tokio::net::TcpStream>, body: Value) {
        socket.send(Message::Text(body.to_string().into())).await.unwrap();
    }

    #[test]
    fn local_sessions_reconcile_never_marks_offline_and_keeps_unlisted_sessions() {
        let mut current = vec![test_native(SID)];
        current[0].status = "busy".into();
        reconcile_sessions(&mut current, vec![]);
        assert_eq!(current.len(), 1, "an unlisted session is kept, not dropped");
        assert_eq!(current[0].status, "busy");
    }

    #[test]
    fn local_sessions_unchanged_process_keeps_its_generation_under_another_name() {
        let dir = tempfile::tempdir().unwrap();
        let key = native_key(&test_native(SID));
        // A generation minted by an older fingerprint formula.
        let mut host = test_host(dir.path(), vec![(key.as_str(), test_mirror(SID, "legacy"))]);
        let mut renamed = test_native(SID);
        renamed.process.executable = "/elsewhere/claude".into();
        assert!(host.observe(vec![renamed.clone()]).unwrap().is_empty());
        assert_eq!(host.mirrors[&key].generation, "legacy");
        assert!(host.mirrors[&key].registration.successor.is_none());
        let mut restarted = renamed;
        restarted.process.birth_micros += 1;
        assert_eq!(host.observe(vec![restarted]).unwrap(), vec![key.clone()]);
    }

    #[test]
    fn local_sessions_registration_names_its_holder_and_never_a_kind() {
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.holder = Some("ab12cd34".into());
        let frame = registration(&mirror);
        assert_eq!(frame["session"]["holderId"], "ab12cd34");
        assert!(frame["session"].get("kind").is_none());
        let mut persisted = serde_json::to_value(&mirror.native).unwrap();
        persisted.as_object_mut().unwrap().remove("holder");
        persisted["background"] = json!(true);
        assert_eq!(
            serde_json::from_value::<NativeSession>(persisted).unwrap().holder,
            None,
            "a mirror saved before explicit sharing has no holder; it registers as a session without one"
        );
    }

    #[test]
    fn local_sessions_stale_hook_cannot_mutate_resumed_process() {
        for event in ["Stop", "SessionEnd"] {
            let dir = tempfile::tempdir().unwrap();
            let mut mirror = test_mirror(SID, "resumed");
            mirror.native.status = "busy".into();
            let old = mirror.native.process.clone();
            mirror.native.process.birth_micros += 1;
            let mut host = test_host(dir.path(), vec![(SID, mirror)]);
            let key = host.mirrors.keys().next().unwrap().clone();
            let before = serde_json::to_value(&host.mirrors[&key]).unwrap();
            let event = lifecycle::parse_datagram(&serde_json::to_vec(&json!({
                "v": 2, "provider": "codex", "sessionId": SID, "event": event,
                "sourceProcess": {"pid": old.pid, "birthMicros": old.birth_micros}
            })).unwrap()).unwrap();
            assert_eq!(host.apply_hook(&event).unwrap(), None);
            assert_eq!(serde_json::to_value(&host.mirrors[&key]).unwrap(), before);
            assert!(host.ended_pending.is_empty());
        }
    }

    #[test]
    fn local_sessions_hook_generation_accepts_native_sessions_and_rejects_legacy_mutations() {
        for provider in [lifecycle::Provider::Claude, lifecycle::Provider::Codex] {
            for held in [false, true] {
                let dir = tempfile::tempdir().unwrap();
                let mut mirror = test_mirror(SID, "current");
                mirror.native.provider = if provider == lifecycle::Provider::Claude { "claude" } else { "codex" }.into();
                mirror.native.holder = held.then(|| "ab12cd34".into());
                mirror.native.job = (provider == lifecycle::Provider::Claude).then(|| "1234abcd".into());
                let mut host = test_host(dir.path(), vec![(SID, mirror)]);
                let key = host.mirrors.keys().next().unwrap().clone();
                let busy = hook(provider.clone(), "UserPromptSubmit", SID);
                assert_eq!(host.apply_hook(&busy).unwrap(), Some(key.clone()));
                // A Claude mirror's status is the engine's.
                let moved = |status| if provider == lifecycle::Provider::Claude { "unknown" } else { status };
                assert_eq!(host.mirrors[&key].native.status, moved("busy"));
                for name in ["Stop", "SessionEnd", "SessionStart", "PermissionRequest", "Notification"] {
                    let mut legacy = hook(provider.clone(), name, SID);
                    legacy.source_process = None;
                    legacy.notification_type = Some("permission_prompt".into());
                    let before = serde_json::to_value(&host.mirrors[&key]).unwrap();
                    assert_eq!(host.apply_hook(&legacy).unwrap(), None);
                    assert_eq!(serde_json::to_value(&host.mirrors[&key]).unwrap(), before);
                }
                let mut stale = hook(provider.clone(), "Stop", SID);
                stale.source_process.as_mut().unwrap().pid += 1;
                assert_eq!(host.apply_hook(&stale).unwrap(), None);
                assert_eq!(host.apply_hook(&hook(provider.clone(), "Stop", SID)).unwrap(), Some(key.clone()));
                assert_eq!(host.mirrors[&key].native.status, moved("idle"));
                assert_eq!(host.apply_hook(&hook(provider.clone(), "SessionEnd", SID)).unwrap(), Some(key.clone()));
                // A process's end is not its conversation's: the engine decides.
                assert_eq!(host.mirrors[&key].ended_at.is_some(), provider == lifecycle::Provider::Codex);
            }
        }
    }

    #[test]
    fn local_sessions_a_claude_hook_finds_a_transcript_written_after_discovery() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.provider = "claude".into();
        let mut host = test_host(dir.path(), vec![(SID, mirror)]);
        let key = host.mirrors.keys().next().unwrap().clone();
        // Discovered before its first turn: no transcript yet, and the hook changes nothing.
        host.apply_hook(&hook(lifecycle::Provider::Claude, "UserPromptSubmit", SID)).unwrap();
        assert!(host.mirrors[&key].native.transcript.is_none());
        let project = dir.path().join(".claude/projects/-w");
        fs::create_dir_all(&project).unwrap();
        let path = project.join(format!("{SID}.jsonl"));
        fs::write(&path, "").unwrap();
        // The turn wrote it: the next hook binds it, without waiting for discovery.
        host.apply_hook(&hook(lifecycle::Provider::Claude, "Stop", SID)).unwrap();
        assert_eq!(host.mirrors[&key].native.transcript.as_ref(), Some(&path));
    }

    #[test]
    fn local_sessions_hook_events_drive_status_and_end() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1"))]);
        let key = host.mirrors.keys().next().unwrap().clone();
        let mut ev = lifecycle::HookEvent {
            provider: lifecycle::Provider::Codex,
            source_process: Some(lifecycle::HookProcess::of(&process(std::process::id()).unwrap())),
            event: "UserPromptSubmit".into(),
            session_id: SID.into(),
            transcript_path: None,
            cwd: None,
            notification_type: None,
            message: None,
            reason: None,
            last_assistant_message: None,
            tool_name: None,
            tool_use_id: None,
            waiting_text: None,
            held: None,
        };
        assert_eq!(host.apply_hook(&ev).unwrap(), Some(key.clone()));
        assert_eq!(host.mirrors[&key].native.status, "busy");
        ev.event = "Stop".into();
        ev.last_assistant_message = Some("done".into());
        host.apply_hook(&ev).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "idle");
        assert_eq!(
            host.mirrors[&key].native.last_assistant_message.as_deref(),
            Some("done")
        );
        // The same session id under the other provider is a different session.
        let other = lifecycle::HookEvent {
            provider: lifecycle::Provider::Claude,
            ..ev.clone()
        };
        assert_eq!(host.apply_hook(&other).unwrap(), None);
        // A session this Mac has not discovered yet is not an error.
        let unknown = lifecycle::HookEvent {
            session_id: SID_B.into(),
            ..ev.clone()
        };
        assert_eq!(host.apply_hook(&unknown).unwrap(), None);
        ev.event = "SessionEnd".into();
        host.apply_hook(&ev).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "ended");
        assert!(host.mirrors[&key].ended_at.is_some() && host.ended_pending == vec![key.clone()]);
        ev.event = "UserPromptSubmit".into();
        assert_eq!(
            host.apply_hook(&ev).unwrap(),
            None,
            "events after end are ignored"
        );
        let stored: Mirror = private_read(&dir.path().join(format!("{key}.json"))).unwrap();
        assert_eq!(stored.native.status, "ended");
        assert_eq!(stored.ended_reason.as_deref(), Some("exited"));
    }

    #[test]
    fn local_sessions_a_restricted_ended_mirror_still_names_its_bound_start_on_the_end() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.session_id = Some(SID.into());
        mirror.native.status = "ended".into();
        // An app restart marks a mirror unverified before liveness finds its process gone.
        mirror.native.restriction = Some("runtime-unverified".into());
        mirror.ended_at = Some(1_700_000_042);
        private_write(
            &dir.path().join(format!("start-{}.json",
                fingerprint(&json!([mirror.native.provider, mirror.native.conversation]).to_string()))),
            &json!({"startId":"11111111-1111-4111-8111-111111111111","provider":mirror.native.provider,
                "conversation":mirror.native.conversation,"launcher":process(std::process::id()).unwrap(),
                "boundGeneration":"gen-1"}),
        )
        .unwrap();
        let mut host = test_host(dir.path(), vec![(SID, mirror)]);
        // Nothing has been acked on this socket: the end waits for the first ack.
        host.ended_pending = vec![SID.into()];
        assert!(host.ended_frame(&mut BTreeSet::new()).unwrap().is_none());
        assert_eq!(host.ended_pending, vec![SID.to_string()]);
        for (relay_reads, expected) in [(false, Value::Null), (true, json!("11111111-1111-4111-8111-111111111111"))] {
            host.ended_pending = vec![SID.into()];
            host.relay_reads_ended_start = Some(relay_reads);
            let (_, frame) = host.ended_frame(&mut BTreeSet::new()).unwrap().unwrap();
            assert_eq!(frame["type"], "local.session.ended");
            assert_eq!(frame["startId"], expected);
        }
        // A new socket may reach another relay version: unknown again.
        host.reset_relay_features();
        assert_eq!(host.relay_reads_ended_start, None);
        assert!(host.begin_ended_recovery(SID).unwrap()["session"].get("startId").is_none());
    }

    #[test]
    fn local_sessions_late_start_hook_reregisters_only_its_generation() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.session_id = Some(SID.into());
        let launcher = process(std::process::id()).unwrap();
        let record_path = dir.path().join(format!("start-{}.json",
            fingerprint(&json!(["codex", SID]).to_string())));
        private_write(&record_path, &json!({"startId":"11111111-1111-4111-8111-111111111111",
            "provider":"codex","conversation":SID,"launcher":launcher,"boundGeneration":null})).unwrap();
        private_write(&dir.path().join("claim-11111111-1111-4111-8111-111111111111.json"),
            &json!({"provider":"codex","conversation":SID})).unwrap();
        let mut host = test_host(dir.path(), vec![(SID, mirror)]);
        let in_flight = host.begin_registration(SID).unwrap();
        assert!(in_flight["session"].get("startId").is_none());
        assert_eq!(host.apply_hook(&hook(lifecycle::Provider::Codex, "SessionStart", SID)).unwrap(), Some(SID.into()));
        let current = &host.mirrors[SID];
        assert_ne!(in_flight["session"]["startId"].as_str(),
            start_links::linked(&host.dir, &current.native, &current.generation).as_deref());
        assert_eq!(host.begin_registration(SID).unwrap()["session"]["startId"],
            "11111111-1111-4111-8111-111111111111");
        host.mirrors.get_mut(SID).unwrap().generation = "gen-2".into();
        assert!(host.begin_registration(SID).unwrap()["session"].get("startId").is_none());
    }

    /// A pending question or permission prompt never reaches the transcript
    /// file, so the hook's own row is the only record a teammate can read.
    #[test]
    fn local_sessions_waiting_hook_injects_one_row_per_distinct_reason() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1"))]);
        let key = host.mirrors.keys().next().unwrap().clone();
        let waiting = |text: &str| lifecycle::HookEvent {
            tool_name: Some("Bash".into()),
            waiting_text: Some(text.into()),
            ..hook(lifecycle::Provider::Codex, "PermissionRequest", SID)
        };
        assert_eq!(host.apply_hook(&waiting("git push")).unwrap(), Some(key.clone()));
        assert_eq!(host.mirrors[&key].native.status, "waiting-approval");
        assert_eq!(
            host.mirrors[&key].queue,
            vec![json!({"kind":"lifecycle","role":"system","text":"Wants to run Bash(git push)",
                "truncated":false,"lifecycle":"approval_pending"})]
        );
        // The same prompt re-fires (a redelivery, or the same command asked again).
        host.apply_hook(&waiting("git push")).unwrap();
        assert_eq!(host.mirrors[&key].queue.len(), 1, "the same reason twice is noise");
        assert_eq!(host.mirrors[&key].after_seq, 0, "the file's cursor is not ours to move");
        host.apply_hook(&waiting("rm -rf /tmp/x")).unwrap();
        assert_eq!(host.mirrors[&key].queue.len(), 2);

        // Answered, then asked the same thing again: that is news, not a repeat.
        host.apply_hook(&hook(lifecycle::Provider::Codex, "UserPromptSubmit", SID))
            .unwrap();
        assert_eq!(host.mirrors[&key].native.status, "busy");
        assert!(host.mirrors[&key].waiting.is_none());
        host.apply_hook(&waiting("rm -rf /tmp/x")).unwrap();
        assert_eq!(host.mirrors[&key].queue.len(), 3);

        // A prompt whose reason the hook could not summarize is still a waiting
        // episode, and the reader still gets a row ("Waiting for input").
        host.apply_hook(&hook(lifecycle::Provider::Codex, "Stop", SID))
            .unwrap();
        let bare = hook(lifecycle::Provider::Codex, "PermissionRequest", SID);
        host.apply_hook(&bare).unwrap();
        assert_eq!(host.mirrors[&key].queue.len(), 4);
        assert_eq!(host.mirrors[&key].queue[3]["text"], "");
        host.apply_hook(&bare).unwrap();
        assert_eq!(
            host.mirrors[&key].queue.len(),
            4,
            "one row per waiting episode, reason or no reason"
        );

        // Identity over wording: a re-delivered question is one row, and the
        // same wording under a new tool-use id is a second prompt.
        host.apply_hook(&hook(lifecycle::Provider::Codex, "Stop", SID))
            .unwrap();
        let asked = |id: &str| lifecycle::HookEvent {
            tool_name: Some("AskUserQuestion".into()),
            tool_use_id: Some(id.into()),
            waiting_text: Some("Tea or coffee? (Tea / Coffee)".into()),
            ..hook(lifecycle::Provider::Codex, "PreToolUse", SID)
        };
        host.apply_hook(&asked("toolu_01aaaa")).unwrap();
        host.apply_hook(&asked("toolu_01aaaa")).unwrap();
        assert_eq!(host.mirrors[&key].queue.len(), 5, "a redelivery keeps its id");
        host.apply_hook(&asked("toolu_01bbbb")).unwrap();
        assert_eq!(
            host.mirrors[&key].queue.len(),
            6,
            "a new prompt reading identically is still a new prompt"
        );

        // One question, both hooks: `PreToolUse` then the `PermissionRequest` the
        // same prompt also fires, carrying no tool-use id. The reader gets it once.
        let also_asked = lifecycle::HookEvent {
            tool_name: Some("AskUserQuestion".into()),
            waiting_text: Some("Tea or coffee? (Tea / Coffee)".into()),
            ..hook(lifecycle::Provider::Codex, "PermissionRequest", SID)
        };
        host.apply_hook(&hook(lifecycle::Provider::Codex, "Stop", SID))
            .unwrap();
        host.apply_hook(&asked("toolu_01cccc")).unwrap();
        host.apply_hook(&also_asked).unwrap();
        assert_eq!(
            host.mirrors[&key].queue.len(),
            7,
            "a question asks once, however many hooks report it"
        );
        assert_eq!(
            host.mirrors[&key].queue[6]["text"], "Tea or coffee? (Tea / Coffee)",
            "and it reads as the question, not as a tool call"
        );

        // Hook delivery is not guaranteed: the permission event alone still has to
        // show the prompt, and still reads as the question.
        host.apply_hook(&hook(lifecycle::Provider::Codex, "Stop", SID))
            .unwrap();
        host.apply_hook(&also_asked).unwrap();
        assert_eq!(host.mirrors[&key].queue.len(), 8, "a lost PreToolUse loses nothing");
        assert_eq!(host.mirrors[&key].queue[7]["text"], "Tea or coffee? (Tea / Coffee)");

        // An open episode is not a reason to swallow the next prompt: a question
        // answered mid-turn can be followed by a real permission prompt before any
        // other transition clears the episode.
        host.apply_hook(&waiting("git commit")).unwrap();
        assert_eq!(host.mirrors[&key].queue.len(), 9, "a different prompt is its own row");
        assert_eq!(host.mirrors[&key].queue[8]["text"], "Wants to run Bash(git commit)");

        let stored: Mirror = private_read(&dir.path().join(format!("{key}.json"))).unwrap();
        assert_eq!(stored.queue.len(), 9, "the injected rows survive a restart");
    }

    #[test]
    fn held_claude_permission_survives_discovery_and_idle_notification() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.provider = "claude".into();
        mirror.native.status = "waiting-approval".into();
        mirror.waiting = Some(WaitingPrompt {
            id: None,
            tool: Some("mcp__lightbulb_native_sessions__send".into()),
            summary: "Lightbulb message".into(),
            held: Some(approval::Held {
                id: "appr-0123456789abcdef0123456789abcdef".into(),
                input_digest: "d".repeat(64),
                reply: String::new(),
                exact: true,
                modes: vec![], full: false, similar: None,
            }),
            private_send: None,
            input: None,
            shipped_full: false,
        });
        let key = native_key(&mirror.native);
        let mut host = test_host(dir.path(), vec![(&key, mirror)]);
        let mut rediscovered = host.mirrors[&key].native.clone();
        rediscovered.status = "busy".into();
        host.observe(vec![rediscovered]).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "waiting-approval");
        let notification = lifecycle::HookEvent {
            notification_type: Some("idle_prompt".into()),
            ..hook(lifecycle::Provider::Claude, "Notification", SID)
        };
        host.apply_hook(&notification).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "waiting-approval");
        assert!(host.mirrors[&key].waiting.as_ref().unwrap().held.is_some());
        host.apply_hook(&hook(lifecycle::Provider::Claude, "Stop", SID)).unwrap();
        assert!(host.mirrors[&key].waiting.is_none());
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        assert_eq!(host.mirrors[&key].native.status, "idle", "the hold is over: the engine's word again");
    }

    /// A command line can carry a token, and the mirror is written to disk.
    #[test]
    fn local_sessions_waiting_row_and_its_persisted_key_are_redacted() {
        const TOKEN: &str = "sk-live-0123456789abcdefghij";
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1"))]);
        let key = host.mirrors.keys().next().unwrap().clone();
        let ev = lifecycle::HookEvent {
            tool_name: Some("Bash".into()),
            waiting_text: Some(format!("curl -H \"Authorization: Bearer {TOKEN}\"")),
            ..hook(lifecycle::Provider::Codex, "PermissionRequest", SID)
        };
        host.apply_hook(&ev).unwrap();
        let row = host.mirrors[&key].queue[0]["text"]
            .as_str()
            .unwrap()
            .to_owned();
        assert!(row.contains("[redacted]") && !row.contains(TOKEN), "{row}");
        assert!(
            row.contains("Wants to run Bash(curl"),
            "which command is blocked is the point: {row}"
        );
        let summary = host.mirrors[&key].waiting.clone().unwrap().summary;
        assert!(
            summary.contains("[redacted]") && !summary.contains(TOKEN),
            "{summary}"
        );
        let stored = fs::read_to_string(dir.path().join(format!("{key}.json"))).unwrap();
        assert!(
            !stored.contains(TOKEN),
            "the dedupe key is persisted; it must not carry the token"
        );
    }

    #[test]
    fn local_sessions_an_idle_stop_is_due_only_for_an_unwatched_session_idle_long_enough() {
        let min = |m: u64| Some(Duration::from_secs(m * 60));
        assert!(idle_stop_due("idle", false, min(60), false), "idle an hour, nobody on it");
        assert!(!idle_stop_due("idle", false, min(59), false), "not yet");
        assert!(!idle_stop_due("idle", false, None, false), "no transcript: quiet for an unknown time");
        for status in ["busy", "waiting-approval", "unknown", "offline", "ended"] {
            assert!(!idle_stop_due(status, false, min(600), false), "{status} is never stopped");
        }
        assert!(!idle_stop_due("idle", true, min(600), false), "someone watches it in Lightbulb");
        // Under memory pressure the wait is shorter, and still never for a watched or busy one.
        assert!(idle_stop_due("idle", false, min(20), true));
        assert!(!idle_stop_due("idle", false, min(19), true));
        assert!(!idle_stop_due("idle", true, min(20), true));
        assert!(!idle_stop_due("busy", false, min(20), true));
    }

    #[test]
    fn local_sessions_a_held_session_idle_an_hour_with_nobody_on_it_ends_idle_stopped() {
        let dir = tempfile::tempdir().unwrap();
        let hour_ago = SystemTime::now() - Duration::from_secs(61 * 60);
        let transcript = |name: &str, at: SystemTime| {
            let path = dir.path().join(name);
            fs::write(&path, "{}\n").unwrap();
            fs::File::options().write(true).open(&path).unwrap().set_modified(at).unwrap();
            path
        };
        let held = |holder: &str, status: &str, written: PathBuf| {
            let mut m = test_mirror(&format!("thread-{holder}"), "generation-1");
            // Claude: a held Codex's end waits to learn how its provider left (`awaits_exit`).
            m.native.provider = "claude".into();
            m.native.holder = Some(holder.into());
            m.native.status = status.into();
            m.native.transcript = Some(written);
            m
        };
        let idle = held("1d1e0001", "idle", transcript("idle.jsonl", hour_ago));
        let busy = held("1d1e0002", "busy", transcript("busy.jsonl", hour_ago));
        let fresh = held("1d1e0003", "idle", transcript("fresh.jsonl", SystemTime::now()));
        // A terminal attached after discovery looked: the holder refuses the stop.
        let joined = held("1d1e0004", "idle", transcript("joined.jsonl", hour_ago));
        // Switched to Only me: the workspace would never be told its end.
        let mut only_me = held("1d1e0005", "idle", transcript("only-me.jsonl", hour_ago));
        only_me.shared = false;
        let keys: Vec<String> = [&idle, &busy, &fresh, &joined, &only_me].iter().map(|m| native_key(&m.native)).collect();
        let (k_idle, k_joined, k_only_me) = (keys[0].clone(), keys[3].clone(), keys[4].clone());
        let mut host = test_host(dir.path(), keys.iter().map(String::as_str).zip([idle, busy, fresh, joined, only_me]).collect());
        // Fake holders: each answers an idle END as given, and says whether it was ever asked.
        use std::io::Write;
        use std::os::unix::net::UnixListener;
        let holder = |id: &str| crate::holder::manager::HolderInfo { id: id.into(), socket: dir.path().join(format!("{id}.sock")),
            pid: 2_147_483_100, provider_pid: 2_147_483_101, argv: vec![], cwd: dir.path().into(), watch: true, type_input: true };
        let answers = |info: &crate::holder::manager::HolderInfo, ended: bool| {
            let listener = UnixListener::bind(&info.socket).unwrap();
            listener.set_nonblocking(true).unwrap();
            std::thread::spawn(move || {
                let deadline = Instant::now() + Duration::from_secs(3);
                while Instant::now() < deadline {
                    let Ok((mut conn, _)) = listener.accept() else {
                        std::thread::sleep(Duration::from_millis(10));
                        continue;
                    };
                    conn.set_nonblocking(false).unwrap();
                    let (kind, hello) = crate::holder::protocol::read_frame(&mut conn).unwrap();
                    assert_eq!((kind, hello.as_slice()), (crate::holder::protocol::HELLO, br#"{"role":"origin","idleEnd":true}"#.as_slice()));
                    let facts = json!({"idleEnded": ended});
                    conn.write_all(&crate::holder::protocol::encode(crate::holder::protocol::HELLO, facts.to_string().as_bytes())).unwrap();
                    return true;
                }
                false
            })
        };
        let unattached: Vec<_> = ["1d1e0001", "1d1e0002", "1d1e0003", "1d1e0004", "1d1e0005"].into_iter().map(holder).collect();
        // A relay that cannot read the reason: nothing is stopped.
        super::super::session_states::set_reads_idle_stopped(false);
        host.stop_idle(unattached.clone()).unwrap();
        assert!(host.ended_pending.is_empty());
        let asked = [answers(&unattached[0], true), answers(&unattached[3], false), answers(&unattached[4], true)];
        super::super::session_states::set_reads_idle_stopped(true);
        host.stop_idle(unattached).unwrap();
        let [idle_asked, joined_asked, only_me_asked] = asked.map(|t| t.join().unwrap());
        assert!(idle_asked && joined_asked, "both due holders are asked");
        assert!(!only_me_asked, "an Only-me session's holder is never asked");
        assert_eq!(host.mirrors[&k_idle].ended_reason.as_deref(), Some("idle-stopped"));
        assert_eq!(host.ended_pending, vec![k_idle.clone()], "only the idle one an hour quiet, whose holder took the stop");
        for key in &keys[1..] {
            assert!(host.mirrors[key].ended_at.is_none() && host.mirrors[key].ended_reason.is_none());
        }
        assert!(host.mirrors[&k_joined].ended_at.is_none() && host.mirrors[&k_only_me].ended_at.is_none());
        // The provider's own exit, reported next (hook or pid), cannot relabel it. (That the app's
        // END is not its person's is session_states' test; `end_reason` reads a relay-feature global
        // other tests flip in parallel, so it is not asserted here.)
        host.end_session(&k_idle, "exited").unwrap();
        assert_eq!(host.mirrors[&k_idle].ended_reason.as_deref(), Some("idle-stopped"));
    }

    #[test]
    fn local_sessions_a_holder_born_before_the_login_session_ends_with_that_reason_first() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "generation-1");
        // Claude: a held Codex's end waits to learn how its provider left (`awaits_exit`).
        mirror.native.provider = "claude".into();
        mirror.native.holder = Some("5717a4ed".into());
        let key = native_key(&mirror.native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        // Never pid 0 or 1: a failed END stops the holder, which signals its process group.
        let holder = crate::holder::manager::HolderInfo { id: "5717a4ed".into(), socket: dir.path().join("none.sock"),
            pid: 2_147_483_000, provider_pid: 2_147_483_001, argv: vec![], cwd: dir.path().into(), watch: true, type_input: true };
        host.end_stranded(vec![holder]).unwrap();
        assert_eq!(host.mirrors[&key].ended_reason.as_deref(), Some("login-session-ended"));
        assert_eq!(host.ended_pending, vec![key.clone()]);
        // The provider's own exit, reported next (hook or pid), cannot relabel it.
        host.end_session(&key, "exited").unwrap();
        assert_eq!(host.mirrors[&key].ended_reason.as_deref(), Some("login-session-ended"));
        assert_eq!(host.ended_pending, vec![key.clone()]);
        // The END the app sent marks the holder asked; the frame still names the login session.
        crate::holder::manager::end_asked_now("5717a4ed");
        assert_eq!(super::super::session_states::end_reason(&host.mirrors[&key]), "login-session-ended");
        // The one ended frame says so too.
        host.mirrors.get_mut(&key).unwrap().session_id = Some(SID.into());
        let (_, frame) = host.ended_frame(&mut BTreeSet::new()).unwrap().expect("its end is told");
        assert_eq!(frame["reason"], "login-session-ended");
        crate::holder::manager::end_asked_forget("5717a4ed");
    }

    #[test]
    fn local_sessions_dead_pid_ends_within_one_tick_and_is_reaped_after_a_day() {
        let dir = tempfile::tempdir().unwrap();
        let mut native = test_native(SID);
        native.process.pid = 2_147_483_000; // no such process
        let key = native_key(&native);
        let mut mirror = test_mirror(
            SID,
            &native.process.generation(),
        );
        mirror.native = native.clone();
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        host.liveness_tick();
        assert_eq!(host.mirrors[&key].native.status, "ended");
        assert_eq!(
            host.mirrors[&key].ended_reason.as_deref(),
            Some("process-exited")
        );
        assert_eq!(host.ended_pending, vec![key.clone()]);
        let file = dir.path().join(format!("{key}.json"));
        assert!(file.exists());
        // A sweep that still lists the process must not resurrect an ended session.
        host.observe(vec![native]).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "ended");
        host.mirrors.get_mut(&key).unwrap().ended_at = Some(0);
        assert_eq!(host.reap(&BTreeSet::new()), vec![key.clone()]);
        assert!(!file.exists() && host.mirrors.is_empty());
    }

    #[test]
    fn local_sessions_an_ended_mirror_with_a_legacy_generation_stays_ended() {
        let dir = tempfile::tempdir().unwrap();
        let native = test_native(SID);
        let key = native_key(&native);
        // Stored under a generation an older formula minted; the process is unchanged.
        let mut mirror = test_mirror(SID, "legacy-generation");
        mirror.native = native.clone();
        mirror.native.status = "ended".into();
        mirror.ended_at = Some(epoch_secs());
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        host.observe(vec![]).unwrap();
        host.observe(vec![native]).unwrap();
        assert!(host.mirrors[&key].ended_at.is_some());
        assert_eq!(
            registration_due(&host.mirrors, &BTreeSet::new(), &BTreeMap::new()),
            None,
            "an ended session is never registered"
        );
    }

    #[test]
    fn local_sessions_codex_desktop_same_process_resumes_after_a_new_active_turn() {
        for registration_pending in [false, true] {
            for end_before_drain in [false, true] {
                let dir = tempfile::tempdir().unwrap();
                let path = dir.path().join("rollout.jsonl");
                let meta = "{\"type\":\"session_meta\",\"payload\":{\"id\":\"aaaabbbb-cccc-4ddd-8eee-ffff00001111\",\"source\":\"vscode\",\"originator\":\"Codex Desktop\",\"cli_version\":\"0.155.0-alpha.2.6\",\"history_mode\":\"paginated\"}}\n";
                let ended_at = epoch_secs();
                let ended_stamp = codex::utc_timestamp(ended_at).unwrap();
                let before_stamp = codex::utc_timestamp(ended_at - 1).unwrap();
                let after_stamp = codex::utc_timestamp(ended_at + 1).unwrap();
                let old = format!("{{\"type\":\"event_msg\",\"timestamp\":\"{before_stamp}.000Z\",\"payload\":{{\"type\":\"task_started\",\"turn_id\":\"old\"}}}}\n");
                let after = format!("{{\"type\":\"event_msg\",\"timestamp\":\"{ended_stamp}.000Z\",\"payload\":{{\"type\":\"task_complete\",\"turn_id\":\"old\"}}}}\n{{\"type\":\"event_msg\",\"timestamp\":\"{after_stamp}.000Z\",\"payload\":{{\"type\":\"task_started\",\"turn_id\":\"new\"}}}}\n");
                fs::write(&path, format!("{meta}{old}{after}")).unwrap();

                let mut native = test_native(SID);
                native.transcript = Some(path);
                native.status = "busy".into();
                let key = native_key(&native);
                let old_generation = native.process.generation();
                let mut mirror = test_mirror(SID, &old_generation);
                mirror.native.status = "ended".into();
                mirror.session_id = Some("session-id".into());
                mirror.registration.acknowledged = Some(old_generation.clone());
                mirror.registration.pending = registration_pending;
                mirror.ended_at = Some(ended_at);
                mirror.ended_at_ms = Some(ended_at * 1000);
                mirror.ended_reason = Some("exited".into());
                mirror.ended_sent = true;
                mirror.pending = Some(json!({"batchId":"old-batch","generation":old_generation,
                    "afterSeq":0,"events":[{"kind":"text","text":"queued under old generation"}]}));
                let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
                host.ended_pending.push(key.clone());

                assert_eq!(host.observe(vec![native]).unwrap(), vec![key.clone()]);
                let resumed = &host.mirrors[&key];
                assert!(resumed.ended_at.is_none());
                assert!(resumed.ended_reason.is_none());
                assert_eq!(resumed.native.status, "busy");
                assert_eq!(resumed.generation, old_generation);
                assert!(resumed.resume_generation.is_some());
                if end_before_drain {
                    host.end_session(&key, "exited").unwrap();
                    let resumed = &host.mirrors[&key];
                    assert!(resumed.ended_at.is_none());
                    assert_eq!(resumed.deferred_end_reason.as_deref(), Some("exited"));
                    assert!(resumed.resume_generation.is_none());
                }
                let resumed = host.mirrors.get_mut(&key).unwrap();
                if registration_pending {
                    resumed.registered(); // the old-generation Register ACK arrives
                }
                assert!(!resumed.advance(), "queued events still carry the old generation");
                resumed.pending = None;
                resumed.queue.clear(); // the cached old-generation event batch is acknowledged
                let advanced = resumed.advance();
                assert_eq!(advanced, !end_before_drain);
                if !end_before_drain {
                    assert_ne!(resumed.generation, old_generation);
                    assert_eq!(resumed.previous_generation.as_deref(), Some(old_generation.as_str()));
                }
                host.finish_deferred_end(&key).unwrap();
                let resumed = &host.mirrors[&key];
                assert_eq!(resumed.ended_at.is_some(), end_before_drain);
                assert_eq!(resumed.generation == old_generation, end_before_drain);
                assert_eq!(host.ended_pending.contains(&key), end_before_drain);
            }
        }
    }

    #[test]
    fn local_sessions_ended_recovery_ignores_a_partial_jsonl_row() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native = observed_fixture(dir.path());
        let transcript = mirror.native.transcript.as_ref().unwrap();

        fs::write(transcript, br#"{"type":"assistant","message":{"content":["unfinished"}}"#)
            .unwrap();
        assert!(!transcript_unread(&mirror));

        fs::write(transcript, format!("{}\n", json!({"type":"assistant"}))).unwrap();
        assert!(transcript_unread(&mirror));
    }

    #[test]
    fn local_sessions_ended_recovery_detects_unread_subagent_journal() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native = observed_fixture(dir.path());
        let parent = mirror.native.transcript.as_ref().unwrap();
        let metadata = fs::metadata(parent).unwrap();
        mirror.cursor.offset = metadata.len();
        mirror.cursor.inode = Some(metadata.ino());
        let child = dir.path().join("child.jsonl");
        fs::write(&child, format!("{}\n", json!({"type":"assistant","content":"final"})))
            .unwrap();
        mirror.subagents.known.insert(
            "agent-1".into(),
            Subagent { path: Some(child), ..Default::default() },
        );
        mirror.ended_at = Some(epoch_secs());

        assert!(needs_ended_recovery(&mirror));
        mirror.subagents.relay_known = true;
        assert!(!transcript_unread(&mirror));
        mirror.subagents.relay = true;
        assert!(transcript_unread(&mirror));
    }

    #[test]
    fn local_sessions_ended_recovery_discovers_untracked_claude_journals() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native = observed_fixture(dir.path());
        let parent = mirror.native.transcript.as_ref().unwrap();
        let metadata = fs::metadata(parent).unwrap();
        mirror.cursor.offset = metadata.len();
        mirror.cursor.inode = Some(metadata.ino());
        let subs = parent.with_extension("").join("subagents");
        fs::create_dir_all(&subs).unwrap();
        fs::write(
            subs.join("agent-child_1.jsonl"),
            format!("{}\n", json!({"type":"assistant"})),
        )
        .unwrap();
        mirror.ended_at = Some(epoch_secs());

        assert!(needs_ended_recovery(&mirror));
    }

    #[test]
    fn local_sessions_a_dead_claude_session_is_not_held_open_by_unfinished_subagents() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native = observed_fixture(dir.path());
        let parent = mirror.native.transcript.as_ref().unwrap();
        let metadata = fs::metadata(parent).unwrap();
        mirror.cursor.offset = metadata.len();
        mirror.cursor.inode = Some(metadata.ino());
        // Killed mid-task: the child never reported an end, and its journal is fully read.
        mirror.subagents.known.insert("agent-1".into(), Subagent::default());
        mirror.ended_at = Some(epoch_secs());

        mirror.native.process = process(std::process::id()).unwrap();
        assert!(needs_ended_recovery(&mirror), "a running session may still hear from its child");
        mirror.native.process.birth_micros += 1; // that process is gone
        assert!(!needs_ended_recovery(&mirror), "a dead session's end is reported at once");
    }

    #[test]
    fn local_sessions_ended_recovery_retries_pathless_codex_children_within_retention() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native = observed_fixture(dir.path());
        mirror.native.provider = "codex".into();
        let parent = mirror.native.transcript.as_ref().unwrap();
        let metadata = fs::metadata(parent).unwrap();
        mirror.cursor.offset = metadata.len();
        mirror.cursor.inode = Some(metadata.ino());
        mirror.subagents.known.insert("child-1".into(), Subagent::default());
        mirror.ended_at = Some(epoch_secs());

        assert!(needs_ended_recovery(&mirror));
        mirror.ended_at = Some(0);
        assert!(!needs_ended_recovery(&mirror));
    }

    #[test]
    fn local_sessions_reconnect_resets_subagent_feature_negotiation() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.subagents.relay = false;
        mirror.subagents.relay_known = true;
        let mut host = test_host(dir.path(), vec![(SID, mirror)]);

        // Only `endedScreens`: it changes no frame without a real ended holder, so tests running
        // beside this one never see it.
        super::super::session_states::read_features(&json!(["endedScreens"]));

        host.reset_relay_features();

        assert!(!super::super::session_states::reads_ended_screens(), "an older relay on the next socket must get no `screens` frame");
        assert!(!host.mirrors[SID].subagents.relay);
        assert!(!host.mirrors[SID].subagents.relay_known);
    }

    #[test]
    fn local_sessions_reaping_keeps_expired_mirrors_with_drainable_backlog() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native = observed_fixture(dir.path());
        mirror.native.status = "ended".into();
        mirror.ended_at = Some(0);
        mirror.ended_sent = false;
        let key = native_key(&mirror.native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);

        assert!(host.reap(&BTreeSet::new()).is_empty());
        assert!(host.mirrors.contains_key(&key));
    }

    #[test]
    fn local_sessions_keeps_completed_post_end_codex_turn_for_discovery() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("codex.jsonl");
        fs::write(
            &transcript,
            concat!(
                "{\"type\":\"event_msg\",\"timestamp\":\"2001-09-09T01:46:40.001Z\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"new-turn\"}}\n",
                "{\"type\":\"event_msg\",\"timestamp\":\"2001-09-09T01:46:41Z\",\"payload\":{\"type\":\"task_complete\"}}\n"
            ),
        )
        .unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.provider = "codex".into();
        mirror.native.transcript = Some(transcript);
        mirror.ended_at = Some(1_000_000_000);
        mirror.ended_at_ms = Some(1_000_000_000_000);
        mirror.native.status = "ended".into();

        assert_eq!(codex::tail_state(mirror.native.transcript.as_ref().unwrap()).0, "idle");
        assert!(codex_turn_active_after_end(&mirror));
    }

    #[test]
    fn local_sessions_reaping_clears_the_per_key_state_a_connection_holds() {
        let dir = tempfile::tempdir().unwrap();
        let mut ended = test_mirror(SID, "gen-1");
        ended.native.status = "ended".into();
        ended.ended_at = Some(0);
        let mut live = test_mirror(SID_B, "gen-2");
        live.session_id = Some(SID_B.into());
        let mut host = test_host(dir.path(), vec![(SID, ended), (SID_B, live)]);
        host.heartbeat_dirty.insert(SID.into());
        host.ended_pending.push(SID.into());
        let mut registered = BTreeSet::from([SID.to_string(), SID_B.to_string()]);
        let listed = |mirrors: &BTreeMap<String, Mirror>, registered: &BTreeSet<String>| {
            device_heartbeats(mirrors, registered, false).unwrap()[0]["sessions"]
                .as_array()
                .unwrap()
                .len()
        };
        assert_eq!(
            listed(&host.mirrors, &registered),
            1,
            "an ended session is registered but never heartbeated"
        );
        for key in host.reap(&BTreeSet::new()) {
            registered.remove(&key);
        }
        assert!(!registered.contains(SID) && !host.mirrors.contains_key(SID));
        assert!(host.heartbeat_dirty.is_empty() && host.ended_pending.is_empty());
        // A registration set outliving its mirror must never be indexed blindly.
        registered.insert(SID.to_string());
        assert_eq!(
            listed(&host.mirrors, &registered),
            1,
            "a reaped key is skipped, not looked up"
        );
    }

    #[test]
    fn local_sessions_liveness_keeps_a_session_whose_successor_is_alive() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.process.pid = 2_147_483_000; // the predecessor is gone
        mirror.registration.successor = Some(test_native(SID)); // its restart is not
        // A frozen registration is exactly why `advance` has not promoted it yet.
        mirror.registration.pending = true;
        let mut host = test_host(dir.path(), vec![(SID, mirror)]);
        host.liveness_tick();
        assert!(
            host.mirrors[SID].ended_at.is_none() && host.ended_pending.is_empty(),
            "a restarted session is not an ended one"
        );
        assert_eq!(host.mirrors[SID].native.status, "unknown");
    }

    #[test]
    fn local_sessions_liveness_ends_a_session_whose_pid_was_reused() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        // The pid answers -- it is this very process -- but it is not the
        // process the session was started in.
        mirror.native.process.birth_micros ^= 1;
        let mut host = test_host(dir.path(), vec![(SID, mirror)]);
        host.liveness_tick();
        assert_eq!(
            host.mirrors[SID].ended_reason.as_deref(),
            Some("process-exited"),
            "a reused pid is not the session's process"
        );
        assert!(host.mirrors[SID].ended_at.is_some());
    }

    /// A Claude conversation's row as the relay knows it: registered at `gen-1`, "Only me" (paused
    /// on the Mac), titled, on a process that has just gone. `job`: a daemon background session.
    fn moved_row(dir: &Path, job: Option<&str>) -> (Host, String, Process) {
        let mut native = test_native(SID);
        native.provider = "claude".into();
        native.app = false;
        native.job = job.map(str::to_owned);
        native.title = Some("Ship row life".into());
        native.process.pid = 2_147_483_000; // gone
        let gone = native.process.clone();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native = native.clone();
        mirror.session_id = Some("5eed0007-5e55-4000-8000-000000000007".into());
        mirror.registration.acknowledged = Some("gen-1".into());
        mirror.shared = false;
        let key = native_key(&native);
        (test_host(dir, vec![(key.as_str(), mirror)]), key, gone)
    }

    /// The same conversation, now on this test process: what discovery lists after the move.
    fn moved_to_here(host: &Host, key: &str) -> NativeSession {
        let mut native = host.mirrors[key].native.clone();
        native.process = process(std::process::id()).unwrap();
        native
    }

    /// The row re-points: same mirror, same Lightbulb session id, same sharing and title, no end
    /// told, and its next registration chains from the generation the relay has.
    fn assert_repointed(host: &mut Host, key: &str) {
        let now = process(std::process::id()).unwrap().generation();
        let m = &host.mirrors[key];
        assert!(m.ended_at.is_none() && host.ended_pending.is_empty(), "no end between the two processes");
        assert_eq!(m.session_id.as_deref(), Some("5eed0007-5e55-4000-8000-000000000007"));
        assert!(m.withheld(), "still Only me");
        assert_eq!(m.generation, now);
        let frame = host.begin_registration(key).unwrap();
        assert_eq!(frame["previousGeneration"], "gen-1", "a continuation of the run the relay has, not a sibling");
        assert_eq!(frame["session"]["shared"], false);
        assert_eq!(frame["session"]["title"], "Ship row life");
        assert_eq!(host.mirrors.values().filter(|m| m.native.conversation == SID).count(), 1, "one row");
    }

    #[test]
    fn local_sessions_a_daemon_respawn_keeps_one_row() {
        // `claude respawn`: the daemon kills the worker and a new one takes the conversation. The
        // old worker's SessionEnd is its own end, and the engine's row is live on the new process.
        let dir = tempfile::tempdir().unwrap();
        let (mut host, key, gone) = moved_row(dir.path(), Some("65ebc88a"));
        let mut end = hook(lifecycle::Provider::Claude, "SessionEnd", SID);
        end.source_process = Some(lifecycle::HookProcess::of(&gone));
        end.reason = Some("other".into());
        host.apply_hook(&end).unwrap();
        host.apply_engine(&engine_live(vec![]));
        assert!(host.mirrors[&key].ended_at.is_none(), "the gap before the successor's record is no end");
        host.apply_engine(&engine_live(vec![engine_row("live")]));
        assert!(host.mirrors[&key].ended_at.is_none(), "the engine runs it");
        let next = moved_to_here(&host, &key);
        host.observe(vec![next]).unwrap();
        assert_repointed(&mut host, &key);
    }

    #[test]
    fn local_sessions_the_agent_views_restart_keeps_one_row() {
        // The agent view restarts a stopped job with `fork:false`: the same id on a new pid.
        let dir = tempfile::tempdir().unwrap();
        let (mut host, key, _) = moved_row(dir.path(), Some("ae0e0fa4"));
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        assert!(host.mirrors[&key].ended_at.is_none());
        let next = moved_to_here(&host, &key);
        host.observe(vec![next]).unwrap();
        assert_repointed(&mut host, &key);
    }

    #[test]
    fn local_sessions_a_resume_in_another_pid_moves_the_row_and_ends_the_one_it_left() {
        // Pid P ran X; `/resume Y` in P moves P to Y. Y's own process had gone: Y's row moves to P,
        // X's ends, by the engine's rows for both.
        const X: &str = "0000000a-0000-4000-8000-00000000000a";
        let dir = tempfile::tempdir().unwrap();
        let (mut host, key, _) = moved_row(dir.path(), None);
        let mut x = test_mirror(X, "x-1");
        x.native.provider = "claude".into();
        x.native.conversation = X.into();
        let x_key = native_key(&x.native);
        host.mirrors.insert(x_key.clone(), x);
        for _ in 0..2 {
            let left = EngineEnded { conversation: X.into(), ..engine_ended(Some("moved-on")) };
            host.apply_engine(&Ok(EngineView { live: vec![engine_row("live")], ended: vec![left] }));
        }
        assert_eq!(host.mirrors[&x_key].ended_reason.as_deref(), Some("exited"), "P left X");
        assert!(host.mirrors[&key].ended_at.is_none(), "P holds Y now");
        host.ended_pending.clear();
        let next = moved_to_here(&host, &key);
        host.observe(vec![next]).unwrap();
        assert_repointed(&mut host, &key);
    }

    #[test]
    fn local_sessions_a_retired_job_ends_and_its_revive_is_the_same_row() {
        // `bg retire …: idle-prompt`: the worker goes and the engine's row ends. The row ends --
        // Resume attaches it (#4404) -- and the revive on a claimed spare (a new pid, hours later)
        // is the same row again.
        let dir = tempfile::tempdir().unwrap();
        let (mut host, key, _) = moved_row(dir.path(), Some("251719c2"));
        host.apply_engine(&engine_live(vec![]));
        host.apply_engine(&engine_live(vec![]));
        assert_eq!(host.mirrors[&key].ended_reason.as_deref(), Some("process-exited"));
        let next = moved_to_here(&host, &key);
        host.observe(vec![next]).unwrap();
        let m = &host.mirrors[&key];
        assert!(m.ended_at.is_none() && m.withheld() && m.session_id.as_deref() == Some("5eed0007-5e55-4000-8000-000000000007"));
        let frame = host.begin_registration(&key).unwrap();
        assert_eq!(frame["previousGeneration"], "gen-1");
        assert_eq!(host.mirrors.len(), 1);
    }

    #[test]
    fn local_sessions_an_attach_client_is_not_a_new_process_of_the_conversation() {
        // `claude attach` is a PTY client of the job's worker and writes no record (checked live,
        // 2026-10-26): the row stays on the worker, generation and all, while it comes and goes.
        let dir = tempfile::tempdir().unwrap();
        let (mut host, key, _) = moved_row(dir.path(), Some("65ebc88a"));
        let here = moved_to_here(&host, &key);
        host.observe(vec![here.clone()]).unwrap();
        host.mirrors.get_mut(&key).unwrap().registered();
        let generation = host.mirrors[&key].generation.clone();
        let mut client = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        host.observe(vec![here.clone()]).unwrap();
        client.kill().unwrap();
        client.wait().unwrap();
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        assert!(host.mirrors[&key].ended_at.is_none());
        assert_eq!(host.mirrors[&key].generation, generation);
    }

    #[test]
    fn local_sessions_an_ended_session_still_drains_its_last_transcript_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror("s", "gen-1");
        mirror.native = observed_fixture(dir.path());
        mirror.native.process.pid = 2_147_483_000; // the process is gone; its tail is not
        mirror.session_id = Some(uuid::Uuid::new_v4().to_string());
        assert_eq!(
            mirror.next_batch().unwrap_err(),
            "runtime-unverified",
            "a live session with a dead process is unverifiable"
        );
        mirror.native.status = "ended".into();
        mirror.ended_at = Some(epoch_secs());
        let batch = mirror
            .next_batch()
            .unwrap()
            .expect("the end of the conversation is still mirrored");
        assert_eq!(batch["events"][0]["text"], "short-lived public output");
    }

    #[test]
    fn local_sessions_restore_loads_ended_mirrors_only_for_reaping_and_marks_live_unknown() {
        let dir = tempfile::tempdir().unwrap();
        let scope = dir.path().join("scope");
        private_dir(&scope).unwrap();
        let persist = |mut mirror: Mirror| {
            let key = native_key(&mirror.native);
            mirror.persist(&scope.join(format!("{key}.json"))).unwrap();
            key
        };
        let live = persist(test_mirror(SID, "gen-1"));
        let mut recent = test_mirror(SID_B, "gen-1");
        recent.native.status = "ended".into();
        recent.ended_at = Some(
            SystemTime::now()
                .duration_since(SystemTime::UNIX_EPOCH)
                .unwrap()
                .as_secs(),
        );
        let recent = persist(recent);
        let mut old = test_mirror("ccccdddd-eeee-4fff-8000-111122223333", "gen-1");
        old.native.status = "ended".into();
        old.ended_at = Some(0);
        let old = persist(old);
        let restored = Host::restore(
            scope.clone(),
            dir.path().join("home"),
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        assert_eq!(restored.mirrors[&live].native.status, "unknown");
        assert_eq!(
            restored.mirrors[&recent].native.status, "ended",
            "an ended mirror is still loaded, for its frame and its reaping"
        );
        assert!(
            !restored.mirrors.contains_key(&old) && !scope.join(format!("{old}.json")).exists(),
            "ended a day ago -> reaped on restore"
        );
        assert_eq!(
            registration_due(&restored.mirrors, &BTreeSet::new(), &BTreeMap::new()).as_deref(),
            Some(live.as_str())
        );
        let live_registered = BTreeSet::from([live]);
        assert_eq!(
            registration_due(&restored.mirrors, &live_registered, &BTreeMap::new()),
            None,
            "an ended mirror is never sent as a registration"
        );
    }

    #[test]
    fn local_sessions_discovery_recovers_readable_history_without_restart() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout.jsonl");
        fs::write(&path, "{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_complete\"}}\n").unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.transcript = Some(path.clone());
        mirror.native.restriction = Some("history-gap".into());
        let key = native_key(&mirror.native);
        let mut observed = mirror.native.clone();
        observed.restriction = None;
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        assert_eq!(host.observe(vec![observed.clone()]).unwrap(), vec![key.clone()]);
        assert!(host.mirrors[&key].native.restriction.is_none());
        assert_eq!(host.mirrors[&key].cursor.offset, fs::metadata(&path).unwrap().len());
        let at_end = host.mirrors[&key].cursor.clone();
        host.mirrors.get_mut(&key).unwrap().native.restriction = Some("history-gap".into());
        host.observe(vec![observed.clone()]).unwrap();
        assert!(host.mirrors[&key].native.restriction.is_none(), "verified EOF needs no new output");
        assert_eq!(host.mirrors[&key].cursor.offset, at_end.offset);
        for contents in ["{bad}\n", "{\"type\":"] {
            fs::write(&path, contents).unwrap();
            let mirror = host.mirrors.get_mut(&key).unwrap();
            mirror.cursor = Cursor::default();
            mirror.native.restriction = Some("history-gap".into());
            host.observe(vec![observed.clone()]).unwrap();
            assert_eq!(host.mirrors[&key].native.restriction.as_deref(), Some("history-gap"));
            assert_eq!(host.mirrors[&key].cursor.offset, 0);
        }
        fs::rename(&path, dir.path().join("previous.jsonl")).unwrap();
        fs::write(&path, "{}\n").unwrap();
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.cursor = at_end.clone();
        mirror.native.restriction = Some("history-gap".into());
        host.observe(vec![observed]).unwrap();
        assert_eq!(host.mirrors[&key].native.restriction.as_deref(), Some("history-gap"));
        assert_eq!(host.mirrors[&key].cursor.offset, at_end.offset, "rotation never resets history");
    }

    #[test]
    fn local_sessions_restore_retries_only_readable_history_gaps() {
        let dir = tempfile::tempdir().unwrap();
        let scope = dir.path().join("scope");
        private_dir(&scope).unwrap();
        let transcript = dir.path().join("rollout.jsonl");
        let valid = format!("{}\n{}\n",
            json!({"timestamp":"2026-09-30T15:52:00Z","type":"compacted",
                "payload":{"replacement_history":["x".repeat(5_550_000)]}}),
            json!({"type":"event_msg","payload":{"type":"task_complete"}}));
        fs::write(&transcript, &valid).unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.transcript = Some(transcript.clone());
        mirror.native.restriction = Some("history-gap".into());
        let key = native_key(&mirror.native);
        let state = scope.join(format!("{key}.json"));
        mirror.persist(&state).unwrap();
        assert!(transcript_path_unread(&transcript, &mirror.cursor));

        let mut restored = Host::restore(scope.clone(), dir.path().into(), Arc::new(AtomicBool::new(false))).unwrap();
        assert_eq!(restored.mirrors[&key].native.restriction.as_deref(), Some("runtime-unverified"));
        let mut observed = mirror.native.clone();
        observed.restriction = None;
        assert_eq!(restored.observe(vec![observed]).unwrap(), vec![key.clone()]);
        assert!(restored.mirrors[&key].native.restriction.is_none());

        for (contents, offset, wrong_inode) in [
            ("{bad}\n".to_owned(), 0, false),
            ("{\"type\":\"event_msg\"".to_owned(), 0, false),
            (valid.clone(), valid.len() as u64 + 1, false),
            (valid.clone(), 0, true),
            (format!("{}\n", json!({"padding":"z".repeat(MAX_JSONL_ROW)})), 0, false),
        ] {
            fs::write(&transcript, contents).unwrap();
            let mut stuck = mirror.clone();
            stuck.cursor.offset = offset;
            stuck.cursor.inode = wrong_inode.then_some(u64::MAX);
            stuck.persist(&state).unwrap();
            let restored = Host::restore(scope.clone(), dir.path().into(), Arc::new(AtomicBool::new(false))).unwrap();
            assert_eq!(restored.mirrors[&key].native.restriction.as_deref(), Some("history-gap"));
        }
    }

    #[test]
    fn local_sessions_failed_registration_write_retries_before_send_and_survives_lost_ack() {
        let dir = tempfile::tempdir().unwrap();
        let scope = dir.path().join("scope");
        let mut a = observed_fixture(dir.path());
        a.transcript = None;
        a.status = "unknown".into();
        let key = native_key(&a);
        let mut host = Host::restore(
            scope.clone(),
            dir.path().into(),
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        host.observe(vec![a.clone()]).unwrap();
        host.begin_registration(&key).unwrap();
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.session_id = Some(uuid::Uuid::new_v4().to_string());
        mirror.registered();
        private_write(&scope.join(format!("{key}.json")), mirror).unwrap();
        let mut b = a;
        b.process.birth_micros += 1;
        host.observe(vec![b.clone()]).unwrap();

        // The real write fails before an envelope can leave begin_registration.
        host.dir = dir.path().join("unavailable");
        assert!(host.begin_registration(&key).is_err());
        host.dir = scope.clone();
        let retry = host.begin_registration(&key).unwrap();
        let stored: Mirror = private_read(&scope.join(format!("{key}.json"))).unwrap();
        assert!(
            stored.registration.pending,
            "retry returned an envelope without a durable outbox"
        );

        // Server accepts this B attempt, but its ACK is lost before restart.
        let server_generation = retry["session"]["generation"].clone();
        drop(host);
        let mut restarted =
            Host::restore(scope, dir.path().into(), Arc::new(AtomicBool::new(false))).unwrap();
        let mut c = b;
        c.process.birth_micros += 1;
        restarted.observe(vec![c]).unwrap();
        let replay = restarted.begin_registration(&key).unwrap();
        assert_eq!(
            replay["session"]["generation"], server_generation,
            "discovery C must not skip accepted B"
        );
        assert_eq!(replay["session"]["status"], "unknown");
        assert_eq!(
            replay["session"]["controlUnavailableReason"],
            "runtime-unverified"
        );
        let mirror = restarted.mirrors.get_mut(&key).unwrap();
        mirror.registered();
        assert!(mirror.advance());
        let next = restarted.begin_registration(&key).unwrap();
        assert_ne!(next["session"]["generation"], server_generation);
        assert_eq!(next["previousGeneration"], server_generation);
    }
    #[test]
    fn local_sessions_late_permission_notification_keeps_the_held_prompt() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.provider = "claude".into();
        let mut host = test_host(dir.path(), vec![(SID, mirror)]);
        let key = host.mirrors.keys().next().unwrap().clone();
        let id = format!("appr-{}", uuid::Uuid::new_v4().simple());
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.native.status = "waiting-approval".into();
        mirror.waiting = Some(WaitingPrompt {
            id: None,
            tool: Some("mcp__lightbulb_native_sessions__list_targets".into()),
            summary: "list_targets {}".into(),
            held: Some(approval::Held {
                id: id.clone(), input_digest: "d".repeat(64), reply: String::new(),
                exact: true, modes: vec![], full: false, similar: None,
            }),
            private_send: None,
            input: None,
            shipped_full: false,
        });
        host.apply_hook(&lifecycle::HookEvent {
            notification_type: Some("permission_prompt".into()),
            ..hook(lifecycle::Provider::Claude, "Notification", SID)
        }).unwrap();
        assert_eq!(
            host.mirrors[&key].waiting.as_ref().unwrap().held.as_ref().unwrap().id,
            id
        );
        host.apply_hook(&hook(lifecycle::Provider::Claude, "Stop", SID)).unwrap();
        assert!(host.mirrors[&key].waiting.is_none(), "a real status change releases the prompt");
    }

    #[test]
    fn local_sessions_held_input_rides_only_a_relay_that_reads_it_and_only_if_it_fits() {
        let mut mirror = test_mirror(SID, "gen-1");
        let id = format!("appr-{}", uuid::Uuid::new_v4().simple());
        let input = json!({"file_path":"/w/a.rs","old_string":"a","new_string":"b"});
        let prompt = WaitingPrompt { id: None, tool: Some("Edit".into()), summary: "Edit a.rs".into(),
            held: Some(approval::Held { id: id.clone(), input_digest: approval::digest(&input), reply: String::new(),
                exact: false, modes: vec![], full: true, similar: None }),
            private_send: None, input: Some(input.clone()), shipped_full: false };
        let row = approval_row("", &prompt);
        assert!(row["tool"]["input"].get("full").is_none() && row["tool"]["input"].get("input").is_none(),
            "the queued row is exactly today's: an older relay never sees the new keys");
        mirror.waiting = Some(prompt.clone());
        let batch = |events: Vec<Value>| json!({"type":"local.session.events","sessionId":SID,"generation":"gen-1",
            "batchId":"batch-1","afterSeq":0,"events":events});
        let wire = attach_approval_input(&mut mirror, batch(vec![row.clone()]));
        assert!(mirror.waiting.as_ref().unwrap().shipped_full, "what was shipped is what may be allowed");
        assert_eq!(wire["events"][0]["tool"]["input"]["full"], true);
        assert_eq!(wire["events"][0]["tool"]["input"]["input"], input);
        assert!(carries_approval_input(&wire) && carries_approval_input(mirror.pending.as_ref().unwrap()));
        // Not the whole call: shipped to look at, not `full`.
        let mut partial = prompt.clone();
        partial.held.as_mut().unwrap().full = false;
        mirror.waiting = Some(partial);
        let wire = attach_approval_input(&mut mirror, batch(vec![row.clone()]));
        assert_eq!(wire["events"][0]["tool"]["input"]["full"], false);
        assert_eq!(wire["events"][0]["tool"]["input"]["input"], input);
        // Too big for the frame (non-ASCII counts 6x): `input` goes, `full` is false, the rest is sent.
        let huge = json!({"content": "\u{e9}".repeat(4 * 1024)});
        mirror.waiting = Some(WaitingPrompt { input: Some(huge), ..prompt.clone() });
        let wire = attach_approval_input(&mut mirror, batch(vec![row.clone()]));
        assert_eq!(wire["events"][0]["tool"]["input"]["full"], false);
        assert!(wire["events"][0]["tool"]["input"].get("input").is_none());
        assert!(!mirror.waiting.as_ref().unwrap().shipped_full, "downgraded for size: deny-only");
        // Only an approval row's `full` marks a batch as needing the feature.
        assert!(!carries_approval_input(&batch(vec![json!({"kind":"tool","tool":{"input":{"full":true}}})])));
        assert!(ascii_wire_len(&wire_batch(wire)).unwrap() <= 16384);
    }

    #[test]
    fn local_sessions_question_full_input_defers_instead_of_being_consumed_partial() {
        let mut mirror = test_mirror(SID, "gen-1");
        let input = json!({"questions":[{"question":"Pick?","options":[{"label":"A","description":"x".repeat(1500)},{"label":"B"}],"multiSelect":false}]});
        assert!(approval::question_relayable(&input));
        let held = approval::Held { id:format!("appr-{}",uuid::Uuid::new_v4().simple()), input_digest:approval::digest(&input), reply:String::new(), exact:false, modes:vec![], full:true, similar:None };
        let prompt = WaitingPrompt {tool:Some("AskUserQuestion".into()),held:Some(held),input:Some(input.clone()),..WaitingPrompt::default()};
        let row = approval_row("Pick?",&prompt);
        mirror.waiting = Some(prompt);
        let batch = |events:Vec<Value>| json!({"type":"local.session.events","sessionId":SID,"generation":"gen-1","batchId":"b","afterSeq":0,"events":events});
        let earlier = json!({"kind":"text","role":"assistant","text":"x".repeat(15000)});
        let deferred = attach_approval_input(&mut mirror,batch(vec![earlier.clone(),row.clone()]));
        assert_eq!(deferred["events"],json!([earlier]),"question stays in the queue for its own frame");
        assert!(!mirror.waiting.as_ref().unwrap().shipped_full);
        let alone = attach_approval_input(&mut mirror,batch(vec![row]));
        assert_eq!(alone["events"][0]["tool"]["input"]["input"],input);
        assert!(mirror.waiting.as_ref().unwrap().shipped_full);
        assert!(ascii_wire_len(&wire_batch(alone)).unwrap() <= 16384);
    }

    /// The hook's side file reaches the waiting prompt; the app re-checks `full` against the digest.
    #[test]
    fn local_sessions_held_prompt_reads_its_input_from_the_private_side_file() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1"))]);
        let key = host.mirrors.keys().next().unwrap().clone();
        let hooks = enrollment::hook_path().parent().unwrap().to_path_buf();
        private_dir(&hooks).unwrap();
        for (shown_digest_ok, expect_full) in [(true, true), (false, false)] {
            let id = format!("appr-{}", uuid::Uuid::new_v4().simple());
            let reply = hooks.join(format!("{id}.sock"));
            let _hook_side = std::os::unix::net::UnixDatagram::bind(&reply).unwrap();
            let raw = json!({"command":"make\nmake test"});
            let shown = if shown_digest_ok { raw.clone() } else { json!({"command":"make"}) };
            let held = approval::Held { id: id.clone(), input_digest: approval::digest(&raw),
                reply: reply.to_string_lossy().into_owned(), exact: false, modes: vec![], full: true, similar: None };
            private_write(&hooks.join(format!("{id}.json")),
                &json!({"approvalId":id,"inputDigest":held.input_digest,"input":shown})).unwrap();
            host.apply_hook(&lifecycle::HookEvent {
                tool_name: Some("Bash".into()),
                waiting_text: Some("make".into()),
                held: Some(held),
                ..hook(lifecycle::Provider::Codex, "PermissionRequest", SID)
            }).unwrap();
            let waiting = host.mirrors[&key].waiting.clone().unwrap();
            assert_eq!(waiting.input, Some(shown));
            assert_eq!(waiting.held.unwrap().full, expect_full);
            assert!(!hooks.join(format!("{id}.json")).exists(), "read once");
            let _ = fs::remove_file(&reply);
        }
    }

    #[test]
    fn local_sessions_private_send_approval_stays_outside_shared_events() {
        let mut mirror = test_mirror(SID, "gen-1");
        let id = format!("appr-{}", uuid::Uuid::new_v4().simple());
        let digest = "d".repeat(64);
        let detail = json!({"approvalId":id,"inputDigest":digest,"destination":"person:bob",
            "text":"private body","requestId":uuid::Uuid::new_v4().to_string()});
        let prompt = WaitingPrompt { id: None, tool: Some("mcp__lightbulb_native_sessions__send".into()),
            summary: "Lightbulb message".into(), held: Some(approval::Held {
                id: id.clone(), input_digest: digest, reply: String::new(), exact: true, modes: vec![], full: false, similar: None,
            }), private_send: Some(detail.clone()), input: None, shipped_full: false };
        mirror.waiting = Some(prompt.clone());
        let batch = json!({"type":"local.session.events","sessionId":SID,"generation":"gen-1",
            "batchId":"batch-1","afterSeq":0,"events":[approval_row("", &prompt)]});
        let wire = wire_batch(attach_private_send(&mut mirror, batch));
        assert_eq!(wire["privateApproval"], detail);
        assert!(!wire["events"].to_string().contains("private body"));
        assert!(!wire["events"].to_string().contains("person:bob"));
    }

    #[test]
    fn local_sessions_lost_approval_channel_does_not_claim_the_agent_is_working() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1"))]);
        let key = host.mirrors.keys().next().unwrap().clone();
        let hooks = enrollment::hook_path().parent().unwrap().to_path_buf();
        private_dir(&hooks).unwrap();
        let id = format!("appr-{}", uuid::Uuid::new_v4().simple());
        let reply = hooks.join(format!("{id}.sock"));
        let hook_side = std::os::unix::net::UnixDatagram::bind(&reply).unwrap();
        hook_side.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let held = approval::Held {
            id: id.clone(),
            input_digest: "d".repeat(64),
            reply: reply.to_string_lossy().into_owned(),
            exact: true,
            modes: vec![], full: false, similar: None,
        };
        let event = lifecycle::HookEvent {
            tool_name: Some("Bash".into()),
            waiting_text: Some("git push".into()),
            held: Some(held.clone()),
            ..hook(lifecycle::Provider::Codex, "PermissionRequest", SID)
        };
        host.apply_hook(&event).unwrap();
        let mut buf = [0u8; 256];
        let n = hook_side.recv(&mut buf).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&buf[..n]).unwrap(), json!({"hold": id}));
        // The row the backend reads is the shared wire fixture, id and digest aside.
        let mut expected: Value = serde_json::from_str(include_str!(
            "../../../../../packages/shared/fixtures/local-session-frames/approval-pending-event.json"
        ))
        .unwrap();
        expected["itemId"] = json!(id);
        expected["tool"]["input"]["inputDigest"] = json!("d".repeat(64));
        assert_eq!(host.mirrors[&key].queue, vec![expected]);
        // Still held: the probe changes nothing.
        assert!(host.approval_tick().is_empty());
        assert_eq!(host.mirrors[&key].native.status, "waiting-approval");
        // The hook can disappear because the terminal answered, the hook crashed,
        // or its hold expired before the release arrived. A missing channel alone
        // cannot prove the terminal is back at work.
        drop(hook_side);
        let _ = fs::remove_file(&reply);
        assert_eq!(host.approval_tick(), vec![key.clone()]);
        assert_eq!(host.mirrors[&key].native.status, "unknown", "no evidence the native prompt was answered");
        assert!(host.mirrors[&key].waiting.as_ref().unwrap().held.is_none());
        assert!(host.mirrors[&key].queue[1].get("itemId").is_none());
        assert!(host.approval_tick().is_empty(), "once, not every tick");
        host.apply_hook(&hook(lifecycle::Provider::Codex, "UserPromptSubmit", SID)).unwrap();
        assert_eq!(host.mirrors[&key].native.status, "busy", "a native lifecycle event can restore a known state");
        assert!(host.mirrors[&key].waiting.is_none());
    }

    /// Gap 12: a Codex thread's open approval, read from its daemon, is held like a hook's prompt
    /// (tagged, answerable, the whole command shipped) and let go once the daemon no longer holds it.
    #[test]
    fn local_sessions_a_codex_daemon_approval_is_held_and_let_go_with_the_daemon() {
        let thread = "01a17b15-0000-7000-8000-00000000c0de";
        let mut mirror = test_mirror(thread, "gen-1");
        mirror.native.status = "waiting-approval".into();
        let request = json!({"method":"item/commandExecution/requestApproval","id":0,"params":{"threadId":thread,"turnId":"u",
            "itemId":"exec-1","startedAtMs":1,"command":"/bin/zsh -lc 'mkdir -p /tmp/x'","cwd":"/tmp",
            "availableDecisions":["accept","acceptForSession","decline","cancel"]}});
        codex::remember_approval(thread, Some(request));
        codex_hold(&mut mirror, true);
        assert!(mirror.waiting.is_none() && mirror.queue.is_empty(), "a session that is not shared waits for its terminal");
        codex_hold(&mut mirror, false);
        let input = json!({"command":"/bin/zsh -lc 'mkdir -p /tmp/x'"});
        let waiting = mirror.waiting.clone().unwrap();
        let held = waiting.held.clone().unwrap();
        assert_eq!(held.id, codex::approval_id(thread, "exec-1"));
        assert_eq!(held.input_digest, approval::digest(&input));
        assert!(held.exact && held.full && daemon_held(&held));
        assert_eq!(held.modes, vec!["session"]);
        assert_eq!((waiting.id.as_deref(), waiting.input.as_ref()), (Some("exec-1"), Some(&input)));
        let row = &mirror.queue[0];
        assert_eq!((row["lifecycle"].as_str(), row["itemId"].as_str()), (Some("approval_pending"), Some(held.id.as_str())));
        assert_eq!(row["tool"]["input"]["modes"], json!(["session"]));
        codex_hold(&mut mirror, false);
        assert_eq!(mirror.queue.len(), 1, "rediscovered, it is the same prompt");
        // Answered in the terminal: the daemon holds nothing, the prompt goes.
        mirror.native.status = "busy".into();
        codex::remember_approval(thread, None);
        codex_hold(&mut mirror, false);
        assert!(mirror.waiting.is_none());
        // A hook's held prompt is never the daemon's to drop.
        mirror.waiting = Some(WaitingPrompt { held: Some(approval::Held { reply: "/x/appr.sock".into(), ..held }), ..waiting });
        codex_hold(&mut mirror, false);
        assert!(mirror.waiting.is_some());
    }

    #[test]
    fn local_sessions_a_held_prompt_the_hook_lets_go_stays_waiting_but_untagged() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1"))]);
        let key = host.mirrors.keys().next().unwrap().clone();
        let hooks = enrollment::hook_path().parent().unwrap().to_path_buf();
        private_dir(&hooks).unwrap();
        let id = format!("appr-{}", uuid::Uuid::new_v4().simple());
        let reply = hooks.join(format!("{id}.sock"));
        let _hook_side = std::os::unix::net::UnixDatagram::bind(&reply).unwrap();
        let held = approval::Held { id: id.clone(), input_digest: "d".repeat(64), reply: reply.to_string_lossy().into_owned(), exact: true, modes: vec![], full: false, similar: None };
        host.apply_hook(&lifecycle::HookEvent {
            tool_name: Some("Bash".into()),
            waiting_text: Some("ls".into()),
            held: Some(held),
            ..hook(lifecycle::Provider::Codex, "PermissionRequest", SID)
        })
        .unwrap();
        let release = lifecycle::parse_datagram(&lifecycle::build_release_datagram(
            "codex",
            &json!({"session_id": SID}),
            &id,
        ))
        .unwrap();
        let before = serde_json::to_value(&host.mirrors[&key]).unwrap();
        let mut stale = release.clone();
        let mut source = lifecycle::HookProcess::of(&host.mirrors[&key].native.process);
        source.birth_micros += 1;
        stale.source_process = Some(source);
        assert_eq!(host.apply_hook(&stale).unwrap(), None);
        let mut wrong_id = release.clone();
        wrong_id.source_process = None;
        wrong_id.held.as_mut().unwrap().id = format!("appr-{}", uuid::Uuid::new_v4().simple());
        assert_eq!(host.apply_hook(&wrong_id).unwrap(), None);
        assert_eq!(serde_json::to_value(&host.mirrors[&key]).unwrap(), before);
        // A v1 release from an older hook can untag only this exact prompt.
        let legacy_wire = json!({"v":1,"provider":"codex",
            "sessionId":SID,"event":"PermissionRelease","approvalId":id});
        let legacy = lifecycle::parse_datagram(&serde_json::to_vec(&legacy_wire).unwrap()).unwrap();
        assert_eq!(host.apply_hook(&legacy).unwrap(), Some(key.clone()));
        let mirror = &host.mirrors[&key];
        assert_eq!(mirror.native.status, "waiting-approval");
        assert!(mirror.waiting.as_ref().unwrap().held.is_none());
        assert!(mirror.queue[1].get("itemId").is_none(), "the untagged row closes the remote answer");
        let _ = fs::remove_file(&reply);
    }

    #[test]
    fn local_sessions_heartbeat_carries_stats_only_to_a_relay_that_reads_them() {
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.session_id = Some(SID.into());
        let mirrors = BTreeMap::from([(SID.to_string(), mirror)]);
        let registered = BTreeSet::from([SID.to_string()]);
        let entry = |with: bool, mirrors: &BTreeMap<String, Mirror>| {
            device_heartbeats(mirrors, &registered, with).unwrap()[0]["sessions"][0].clone()
        };
        assert!(entry(true, &mirrors).get("stats").is_none(), "nothing reported, nothing sent");
        let mut mirrors = mirrors;
        mirrors.get_mut(SID).unwrap().stats.observe(
            "claude",
            &json!({"type":"permission-mode","permissionMode":"plan"}),
        );
        assert!(entry(false, &mirrors).get("stats").is_none(), "an older relay rejects the field");
        assert_eq!(entry(true, &mirrors)["stats"]["permissionMode"], "plan");
    }

    #[test]
    fn local_sessions_question_answers_bind_full_prompt_and_passive_hooks_cannot_revoke_it() {
        let input = json!({"questions":[{"question":"Pick?","options":[{"label":"A"},{"label":"B"}],"multiSelect":false}]});
        let held = approval::Held { id: "appr-0123456789abcdef0123456789abcdef".into(), input_digest: approval::digest(&input), reply: String::new(), exact:false, full:true, modes:vec![], similar:None };
        let passive = WaitingPrompt { tool:Some("AskUserQuestion".into()), summary:"Pick?".into(), ..WaitingPrompt::default() };
        let mut prompt = WaitingPrompt { held:Some(held.clone()), input:Some(input), shipped_full:true, ..passive.clone() };
        assert!(passive.replace_with(&prompt), "held report upgrades passive PreToolUse");
        assert!(!prompt.replace_with(&passive), "late passive PreToolUse keeps held PermissionRequest");
        let next = WaitingPrompt { held:Some(approval::Held { id:"appr-11111111111111111111111111111111".into(), ..held.clone() }), ..prompt.clone() };
        assert!(prompt.replace_with(&next), "same words with a new held id are a new question");
        let cmd: Command = serde_json::from_value(json!({"requestId":"r1","expectedGeneration":"g1","kind":"answer","promptId":held.id,"inputDigest":held.input_digest,"answers":{"Pick?":"A"},"text":null,"expectedTurnId":null,"sessionId":"s1","roomId":"","actorUserId":"alice","commandSeq":1,"state":"dispatched","createdAt":100,"deliverBy":130,"held":false,"providerMessageId":null})).unwrap();
        assert!(question_answerable(&prompt,&held,&cmd));
        let mut wrong = cmd.clone(); wrong.prompt_id = Some("old".into());
        assert!(!question_answerable(&prompt,&held,&wrong));
        wrong = cmd.clone(); wrong.input_digest = Some("e".repeat(64));
        assert!(!question_answerable(&prompt,&held,&wrong));
        wrong = cmd.clone(); wrong.answers = Some(json!({"Other?":"A"}));
        assert!(!question_answerable(&prompt,&held,&wrong));
        wrong = cmd.clone(); wrong.mode = Some("acceptEdits".into());
        assert!(!question_answerable(&prompt,&held,&wrong));
        prompt.shipped_full = false;
        assert!(!question_answerable(&prompt,&held,&cmd));
        let mut native = test_mirror(SID,"gen-1").native;
        native.provider = "claude".into(); native.restriction = Some("history-gap".into());
        assert!(question_runtime(&native,Some(&native.process)), "current held question does not depend on older transcript history");
        native.restriction = Some("runtime-unverified".into());
        assert!(question_runtime(&native,Some(&native.process)), "hook and process identity prove this narrow capability");
        assert!(!question_runtime(&native,None), "a dead or unreadable process cannot be answered");
        let mut stale = native.process.clone(); stale.birth_micros += 1;
        assert!(!question_runtime(&native,Some(&stale)), "PID reuse must not reach a successor");
        native.restriction = Some("sharing-off".into());
        assert!(!question_runtime(&native,Some(&native.process)), "other restrictions still block");

    }

    #[test]
    fn local_sessions_approve_frame_parses_and_answers_only_its_exact_prompt() {
        let frame: Value = serde_json::from_str(include_str!(
            "../../../../../packages/shared/fixtures/local-session-frames/approve-command.json"
        ))
        .unwrap();
        let cmd = parse_command(&frame, "user_alice").unwrap();
        assert_eq!(cmd.kind, "approve");
        assert!(!cmd.own, "a teammate's answer is never the owner's");
        let held = approval::Held {
            id: cmd.approval_id.clone().unwrap(),
            input_digest: cmd.input_digest.clone().unwrap(),
            reply: String::new(),
            exact: true,
            modes: vec!["acceptEdits".into()], full: false, similar: None,
        };
        assert!(answerable(&held, true, &cmd));
        assert!(!answerable(&approval::Held { exact: false, ..held.clone() }, true, &cmd), "inexact is deny-only");
        assert!(answerable(&approval::Held { exact: false, full: true, ..held.clone() }, true, &cmd), "shown whole");
        assert!(
            !answerable(&approval::Held { exact: false, full: true, ..held.clone() }, false, &cmd),
            "full on the Mac, but the row that shipped was not: deny-only"
        );
        // The held id names one prompt and so its tool: an answer for prompt A, same input
        // digest and all, never allows prompt B.
        let other = approval::Held { id: format!("appr-{}", uuid::Uuid::new_v4().simple()), full: true, ..held.clone() };
        assert!(!answerable(&other, true, &cmd));
        assert!(!answerable(&approval::Held { input_digest: "e".repeat(64), ..held.clone() }, true, &cmd));
        let mut deny = cmd.clone();
        deny.decision = Some("deny".into());
        assert!(answerable(&approval::Held { exact: false, ..held.clone() }, true, &deny));
        let mut widened = cmd.clone();
        widened.mode = Some("plan".into());
        assert!(!answerable(&held, true, &widened), "only a mode the prompt offered");
        let mut agent: Command = serde_json::from_value(json!({"requestId":"r2","expectedGeneration":"g1","kind":"approve",
            "approvalId":held.id,"decision":"allow","inputDigest":held.input_digest,"expectedTurnId":null,"text":null,
            "sessionId":"s","roomId":"room","actor":{"kind":"agent","id":"a1"},"invocationExpiresAt":2000,
            "commandSeq":2,"state":"dispatched","createdAt":1000,"deliverBy":1030,"held":false,"providerMessageId":null})).unwrap();
        assert!(!answerable(&held, true, &agent), "an agent never answers a prompt");
        agent.actor = None;
        assert!(!answerable(&held, true, &agent), "nor does a command with no person behind it");
        // A send serializes exactly as before approvals existed: its journal hash is stable.
        let send = json!({"type":"local.session.command","command":{"requestId":"r1","expectedGeneration":"g1","kind":"send","text":"hello",
            "expectedTurnId":null,"sessionId":"s1","roomId":"","actorUserId":"alice","commandSeq":1,"state":"dispatched","createdAt":100,"deliverBy":130,"held":false,"providerMessageId":null}});
        let wire = serde_json::to_value(parse_command(&send, "alice").unwrap()).unwrap();
        assert!(wire.get("approvalId").is_none() && wire.get("decision").is_none());
        // The journal admits an answer the way it admits a send, and never twice.
        let journal = Journal { dir: tempfile::tempdir().unwrap().keep() };
        assert!(journal.admit(&cmd, "g1", 1001).unwrap());
        assert!(!journal.admit(&cmd, "g1", 1001).unwrap());
    }

    #[test]
    fn local_sessions_command_frame_label_is_optional_and_never_trusted_for_identity() {
        let frame = json!({"type":"local.session.command","command":{"requestId":"r1","expectedGeneration":"g1","kind":"send","text":"hello",
            "expectedTurnId":null,"sessionId":"s1","roomId":"room","actorUserId":"alice","commandSeq":1,"state":"dispatched","createdAt":100,"deliverBy":130,"held":false,"providerMessageId":null},
            "actor":{"kind":"human","handle":"aden","displayName":"Aden Kim"}});
        let cmd = parse_command(&frame, "bob").unwrap();
        assert_eq!(
            cmd.label.as_ref().and_then(|l| l.handle.as_deref()),
            Some("aden")
        );
        assert_eq!(cmd.actor_user_id.as_deref(), Some("alice"));
        assert!(!cmd.own);
        assert!(
            parse_command(&frame, "alice").unwrap().own,
            "the enrolled human's own prompt"
        );
        let mut bare = frame.clone();
        bare.as_object_mut().unwrap().remove("actor");
        assert!(parse_command(&bare, "bob").unwrap().label.is_none());
        let mut forged = frame;
        forged["actor"] = json!("not-an-object");
        assert!(parse_command(&forged, "bob").unwrap().label.is_none());
    }
    #[test]
    fn local_sessions_failed_observation_write_retries_identical_snapshot() {
        // Both shapes of a failed write: a first sighting, and a status the sweep
        // moved on a session that is already mirrored.
        for known in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let scope = dir.path().join("scope");
            let native = observed_fixture(dir.path());
            let key = native_key(&native);
            let mut host = Host::restore(
                scope.clone(),
                dir.path().into(),
                Arc::new(AtomicBool::new(false)),
            )
            .unwrap();
            if known {
                host.observe(vec![native.clone()]).unwrap();
            }
            let mut listed = native;
            if known {
                listed.status = "busy".into();
            }
            let update = vec![listed];
            host.dir = dir.path().join("unavailable");
            assert!(host.observe(update.clone()).is_err());
            host.dir = scope.clone();
            host.observe(update).unwrap();
            let stored: Mirror = private_read(&scope.join(format!("{key}.json")))
                .expect("identical observation must retry the failed write");
            assert_eq!(stored.native.status, if known { "busy" } else { "unknown" });
            assert_eq!(stored.queue[0]["text"], "short-lived public output");
        }
    }
    #[test]
    fn local_sessions_failed_batch_and_ack_writes_retry_without_new_data() {
        let dir = tempfile::tempdir().unwrap();
        let scope = dir.path().join("scope");
        let native = observed_fixture(dir.path());
        let key = native_key(&native);
        let path = scope.join(format!("{key}.json"));
        let unavailable = dir.path().join("unavailable");
        let mut host = Host::restore(
            scope.clone(),
            dir.path().into(),
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        host.observe(vec![native]).unwrap();
        let mirror = host.mirrors.get_mut(&key).unwrap();
        mirror.session_id = Some(uuid::Uuid::new_v4().to_string());
        mirror.registered();
        mirror.persist(&path).unwrap();

        let before = serde_json::to_value(&*mirror).unwrap();
        let batch = capture_batch(mirror).unwrap().unwrap();
        assert!(persist_changed(&unavailable, &key, mirror, &before).is_err());
        let unchanged = serde_json::to_value(&*mirror).unwrap();
        persist_changed(&scope, &key, mirror, &unchanged).unwrap();
        let stored: Mirror = private_read(&path).unwrap();
        assert_eq!(
            stored.pending,
            Some(batch.clone()),
            "retry must durably retain the exact upload envelope"
        );

        mirror
            .acknowledge(batch["batchId"].as_str().unwrap(), 1)
            .unwrap();
        assert!(mirror
            .persist(&unavailable.join(format!("{key}.json")))
            .is_err());
        let unchanged = serde_json::to_value(&*mirror).unwrap();
        persist_changed(&scope, &key, mirror, &unchanged).unwrap();
        let stored: Mirror = private_read(&path).unwrap();
        assert_eq!(stored.after_seq, 1);
        assert!(stored.pending.is_none() && stored.queue.is_empty());
        let inode = fs::metadata(&path).unwrap().ino();
        persist_changed(&scope, &key, mirror, &unchanged).unwrap();
        assert_eq!(
            fs::metadata(&path).unwrap().ino(),
            inode,
            "successful retry must clear the write obligation"
        );
        assert!(serde_json::to_value(&*mirror)
            .unwrap()
            .get("dirty")
            .is_none());
    }
    #[test]
    fn local_sessions_generation_outbox_retries_ambiguous_b_before_c_after_restart() {
        for committed in [false, true] {
            let dir = tempfile::tempdir().unwrap();
            let scope = dir.path().join("scope");
            let mut a = observed_fixture(dir.path());
            a.transcript = None;
            a.status = "unknown".into();
            let key = native_key(&a);
            let mut host = Host::restore(
                scope.clone(),
                dir.path().into(),
                Arc::new(AtomicBool::new(false)),
            )
            .unwrap();
            host.observe(vec![a.clone()]).unwrap();
            let ag = host.mirrors[&key].generation.clone();
            host.mirrors.get_mut(&key).unwrap().session_id = Some(uuid::Uuid::new_v4().to_string());
            let mut b = a.clone();
            b.process.birth_micros += 1;
            host.observe(vec![b.clone()]).unwrap();
            let attempt = host.begin_registration(&key).unwrap();
            assert_eq!(attempt["previousGeneration"], ag);
            let mut server_generation = if committed {
                attempt["session"]["generation"]
                    .as_str()
                    .unwrap()
                    .to_owned()
            } else {
                ag
            };
            let mut c = b;
            c.process.birth_micros += 1;
            host.observe(vec![c.clone()]).unwrap();
            drop(host);
            let mut restarted =
                Host::restore(scope, dir.path().into(), Arc::new(AtomicBool::new(false))).unwrap();
            let retry = restarted.begin_registration(&key).unwrap();
            assert_eq!(
                retry["session"]["generation"], attempt["session"]["generation"],
                "must retry B, not skip to C"
            );
            assert!(
                retry["session"]["generation"] == server_generation
                    || retry["previousGeneration"] == server_generation,
                "canonical CAS"
            );
            assert_eq!(
                retry["session"]["status"], "unknown",
                "a restart verifies nothing, but it does not declare it offline either"
            );
            assert_eq!(
                retry["session"]["controlUnavailableReason"],
                "runtime-unverified"
            );
            server_generation = retry["session"]["generation"].as_str().unwrap().into();
            // The real ACK path will durably retire B before advancing C.
            let mirror = restarted.mirrors.get_mut(&key).unwrap();
            mirror.registered();
            assert!(mirror.advance());
            private_write(&restarted.dir.join(format!("{key}.json")), mirror).unwrap();
            let mut restarted = Host::restore(
                restarted.dir.clone(),
                dir.path().into(),
                Arc::new(AtomicBool::new(false)),
            )
            .unwrap();
            let next = restarted.begin_registration(&key).unwrap();
            assert_ne!(next["session"]["generation"], server_generation);
            assert_eq!(next["previousGeneration"], server_generation);
        }
    }
    fn observed_fixture(dir: &Path) -> NativeSession {
        let transcript = dir.join("native.jsonl");
        fs::write(&transcript,format!("{}\n",json!({"type":"assistant","sessionId":"s","version":"2.1.268","message":{"role":"assistant","content":[{"type":"text","text":"short-lived public output"}]}}))).unwrap();
        NativeSession {
            provider: "claude".into(),
            conversation: "s".into(),
            profile: "f".repeat(64),
            process: process(std::process::id()).unwrap(),
            transcript: Some(transcript),
            socket: None,
            socket_inode: None,
            version: "2.1.268".into(),
            restriction: None,
            status: "unknown".into(),
            title: None,
            cwd_name: None,
            active_turn: None,
            last_assistant_message: None,
            holder: None,
            job: None,
            forked_from: None,
            forked_by_person: None,
            aliases: vec![],
            app: true,
            attention: Default::default(),
        }
    }
    #[tokio::test]
    async fn local_sessions_outage_keeps_multiple_discovery_passes_and_short_lived_history() {
        let dir = tempfile::tempdir().unwrap();
        let scope = dir.path().join("scope");
        let native = observed_fixture(dir.path());
        let key = native_key(&native);
        let mut host = Host::restore(
            scope.clone(),
            dir.path().into(),
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (recovered, offline) = tokio::sync::oneshot::channel();
        let producer = tokio::spawn(async move {
            tx.send((vec![], vec![], vec![], epoch_ms())).await.unwrap();
            tx.send((vec![], vec![], vec![], epoch_ms())).await.unwrap();
            tx.send((vec![native], vec![], vec![], epoch_ms())).await.unwrap();
            tx.send((vec![], vec![], vec![], epoch_ms())).await.unwrap();
            // Wait until the exited-session pass is consumed, not just queued.
            let permit = tx.reserve().await.unwrap();
            drop(permit);
            recovered.send(()).unwrap();
            tx
        });
        tokio::time::timeout(
            Duration::from_secs(1),
            host.observing_wait(&mut rx, async {
                offline.await.map_err(|_| "test relay closed")?;
                Ok(())
            }),
        )
        .await
        .unwrap()
        .unwrap();
        let _sender = producer.await.unwrap();
        let stored: Mirror = private_read(&scope.join(format!("{key}.json"))).unwrap();
        assert!(stored.ended_at.is_none(), "an empty listing never ends a live session");
        assert!(stored.cursor.offset > 0);
        assert_eq!(stored.queue[0]["text"], "short-lived public output");
        let mut restarted =
            Host::restore(scope, dir.path().into(), Arc::new(AtomicBool::new(false))).unwrap();
        let mirror = restarted.mirrors.get_mut(&key).unwrap();
        mirror.session_id = Some(uuid::Uuid::new_v4().to_string());
        assert_eq!(
            mirror.next_batch().unwrap().unwrap()["events"][0]["text"],
            "short-lived public output"
        );
    }
    #[test]
    fn local_sessions_observation_does_not_rewrite_unchanged_or_unlisted_state() {
        let dir = tempfile::tempdir().unwrap();
        let scope = dir.path().join("scope");
        // Listed with a known status: an unlisted live "unknown" is moved to idle, a change.
        let mut native = observed_fixture(dir.path());
        native.status = "idle".into();
        let key = native_key(&native);
        let mut host = Host::restore(
            scope.clone(),
            dir.path().into(),
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        host.observe(vec![native.clone()]).unwrap();
        let path = scope.join(format!("{key}.json"));
        let inode = fs::metadata(&path).unwrap().ino();
        for _ in 0..10 {
            host.observe(vec![native.clone()]).unwrap();
        }
        assert_eq!(fs::metadata(&path).unwrap().ino(), inode);
        for _ in 0..10 {
            host.observe(vec![]).unwrap();
        }
        assert_eq!(
            fs::metadata(&path).unwrap().ino(),
            inode,
            "an empty listing is not a change"
        );
        let mut busy = native;
        busy.status = "busy".into();
        host.observe(vec![busy.clone()]).unwrap();
        let moved = fs::metadata(&path).unwrap().ino();
        assert_ne!(moved, inode, "the listing's status is durable");
        for _ in 0..10 {
            host.observe(vec![busy.clone()]).unwrap();
        }
        assert_eq!(fs::metadata(path).unwrap().ino(), moved);
    }
    #[tokio::test]
    #[ignore = "requires explicitly owned disposable native PID/thread; remote boundary is a local mock"]
    async fn local_sessions_owned_native_command_and_public_mirror_through_production_transport() {
        let home = PathBuf::from(std::env::var("HOME").unwrap());
        let provider = std::env::var("LIGHTBULB_TEST_PROVIDER").unwrap();
        assert!(matches!(provider.as_str(), "claude" | "codex"));
        let pid: u32 = std::env::var("LIGHTBULB_TEST_NATIVE_PID")
            .unwrap()
            .parse()
            .unwrap();
        let conversation = std::env::var("LIGHTBULB_TEST_NATIVE_THREAD").unwrap();
        let program = provider_program(&home, &provider).unwrap();
        let native = if provider == "claude" {
            let rows: Vec<Value> = serde_json::from_str(
                &native_output(&program, &["agents", "--json"])
                    .await
                    .unwrap(),
            )
            .unwrap();
            claude::capture(
                &home,
                &program,
                &rows
                    .into_iter()
                    .find(|r| r["pid"] == pid)
                    .expect("owned PID must be live"),
            )
            .await
            .unwrap()
        } else {
            codex::capture(&home, &program, pid)
                .await
                .unwrap()
                .into_iter()
                .find(|s| s.conversation == conversation)
                .unwrap()
        };
        assert_eq!(native.conversation, conversation);
        assert_eq!(native.restriction, None);
        let generation = native.process.generation();
        let sid = uuid::Uuid::new_v4().to_string();
        let request = uuid::Uuid::new_v4().to_string();
        let marker = format!(
            "HOST_INTEGRATED_{}_{}",
            provider.to_uppercase(),
            uuid::Uuid::new_v4().simple()
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let expected_generation = generation.clone();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let mut after = 0;
            let mut admitted = false;
            let mut mirrored = false;
            let mut sent = false;
            let started = Instant::now();
            while started.elapsed() < Duration::from_secs(60) {
                let frame = tokio::time::timeout(Duration::from_secs(15), socket.next())
                    .await
                    .unwrap()
                    .unwrap()
                    .unwrap();
                let Message::Text(frame) = frame else {
                    continue;
                };
                let body: Value = serde_json::from_str(&frame).unwrap();
                match body["type"].as_str().unwrap() {
                    "local.session.register" | "local.device.heartbeat" => {
                        let registration = body["type"] == "local.session.register";
                        let now = SystemTime::now()
                            .duration_since(SystemTime::UNIX_EPOCH)
                            .unwrap()
                            .as_secs();
                        if !sent {
                            sent = true;
                            socket.send(Message::Text(json!({"type":"local.session.command","command":{
                                "requestId":request,"expectedGeneration":expected_generation,"kind":"send","text":format!("Reply exactly {marker} and do not use tools."),
                                "expectedTurnId":null,"sessionId":sid,"roomId":"owned-test-room","actorUserId":"owned-test-human",
                                "commandSeq":1,"state":"dispatched","createdAt":now,"deliverBy":now+30,"held":false,"providerMessageId":null
                            }}).to_string().into())).await.unwrap();
                        }
                        let pending = sent && !admitted;
                        let ack = if registration {
                            json!({"type":"local.session.registered","sessionId":sid,"generation":expected_generation,
                                "afterSeq":after,"serverTime":now,
                                "reconcile":if pending {json!({"requestId":request,"generation":expected_generation,"state":"unknown"})} else {Value::Null}})
                        } else {
                            json!({"type":"local.device.heartbeat.ack","rejected":[],"serverTime":now,
                                "reconcile":if pending {json!([{"sessionId":sid,"requestId":request,"generation":expected_generation,"state":"unknown"}])} else {json!([])}})
                        };
                        socket.send(Message::Text(ack.to_string().into())).await.unwrap();
                    }
                    "local.session.receipt" => {
                        assert_ne!(
                            body["outcome"], "rejected",
                            "verified owned session must admit the request"
                        );
                        admitted |= body["outcome"] == "applied";
                        if admitted {
                            assert!(body["providerMessageId"]
                                .as_str()
                                .is_some_and(|id| uuid::Uuid::parse_str(id).is_ok()));
                        }
                    }
                    "local.session.events" => {
                        after += 1;
                        mirrored |= body["events"].as_array().unwrap().iter().any(|e| {
                            e["role"] == "assistant"
                                && e["text"].as_str().is_some_and(|t| t.contains(&marker))
                        });
                        socket.send(Message::Text(json!({"type":"local.session.events.ack","sessionId":sid,"batchId":body["batchId"],"afterSeq":after}).to_string().into())).await.unwrap();
                    }
                    other => panic!("unexpected frame {other}"),
                }
                if admitted && mirrored {
                    break;
                }
            }
            assert!(admitted, "native admission was not proven");
            assert!(mirrored, "public response was not mirrored");
            socket.close(None).await.unwrap();
        });
        let dir = tempfile::tempdir().unwrap();
        let mirror = Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native,
            generation,
            previous_generation: None,
            session_id: None,
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            shared: true,
            stop_owed: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
            network_id: None,
        };
        let mut host = Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.path().into(),
            home,
            mirrors: BTreeMap::from([("owned".into(), mirror)]),
            stop: Arc::new(AtomicBool::new(false)),
            hooks: None,
            rediscover: None,
            ended_pending: vec![],
            relay_reads_ended_start: None,
            engine: None,
            observed_at: None,
            discovery_read_ms: None,
            engine_missed: BTreeSet::new(),
            left_on_purpose: BTreeSet::new(),
            engine_woken: BTreeSet::new(),
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
            verify: verify_native,
            share_every: Some(true),
        };
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
    }
    #[test]
    fn local_sessions_restart_restores_pending_upload_without_node_binary() {
        let dir = tempfile::tempdir().unwrap();
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let native = NativeSession {
            provider: "codex".into(),
            conversation: "native".into(),
            profile: "f".repeat(64),
            process: process(std::process::id()).unwrap(),
            transcript: None,
            socket: None,
            socket_inode: None,
            version: "0.149.0".into(),
            restriction: None,
            status: "unknown".into(),
            title: None,
            cwd_name: None,
            active_turn: None,
            last_assistant_message: None,
            holder: None,
            job: None,
            forked_from: None,
            forked_by_person: None,
            aliases: vec![],
            app: true,
            attention: Default::default(),
        };
        let key = native_key(&native);
        let mirror = Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native,
            generation: "g1".into(),
            previous_generation: None,
            session_id: Some("session".into()),
            cursor: Cursor::default(),
            after_seq: 2,
            queue: vec![json!({"kind":"text","role":"assistant","text":"saved","truncated":false})],
            pending: Some(json!({"batchId":"stable","afterSeq":2})),
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            shared: true,
            stop_owed: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
            network_id: None,
        };
        private_write(&dir.path().join(format!("{key}.json")), &mirror).unwrap();
        let host = Host::restore(
            dir.path().into(),
            dir.path().join("no-provider-or-node-binary"),
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        assert_eq!(host.mirrors[&key].pending, mirror.pending);
        assert_eq!(host.mirrors[&key].native.status, "unknown");
    }
    #[tokio::test]
    async fn local_sessions_transport_registers_then_uploads_and_persists_ack() {
        let dir = tempfile::tempdir().unwrap();
        let persisted = dir.path().join("local.json");
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let frame = tokio::time::timeout(Duration::from_secs(2), socket.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            let registration: Value = serde_json::from_str(frame.to_text().unwrap()).unwrap();
            assert_eq!(registration["type"], "local.session.register");
            assert_eq!(registration["session"]["status"], "unknown");
            assert!(registration["session"].get("transcript").is_none());
            socket.send(Message::Text(json!({"type":"local.session.command","command":{
                "requestId":"late","expectedGeneration":"old","kind":"send","text":"must not reach native",
                "expectedTurnId":null,"sessionId":"aaaabbbb-cccc-4ddd-8eee-ffff00001111","roomId":"room","actorUserId":"alice",
                "commandSeq":1,"state":"dispatched","createdAt":50,"deliverBy":80,"held":false,"providerMessageId":null
            }}).to_string().into())).await.unwrap();
            socket.send(Message::Text(json!({"type":"local.session.registered","sessionId":"aaaabbbb-cccc-4ddd-8eee-ffff00001111",
                "generation":"g1","afterSeq":0,"serverTime":100,"reconcile":null}).to_string().into())).await.unwrap();
            let frame = tokio::time::timeout(Duration::from_secs(2), socket.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            let receipt: Value = serde_json::from_str(frame.to_text().unwrap()).unwrap();
            assert_eq!(receipt["type"], "local.session.receipt");
            assert_eq!(receipt["outcome"], "rejected");
            assert_eq!(receipt["generation"], "old");
            let frame = tokio::time::timeout(Duration::from_secs(2), socket.next())
                .await
                .unwrap()
                .unwrap()
                .unwrap();
            let batch: Value = serde_json::from_str(frame.to_text().unwrap()).unwrap();
            assert_eq!(batch["type"], "local.session.events");
            assert_eq!(batch["events"][0]["text"], "public output");
            socket.send(Message::Text(json!({"type":"local.session.events.ack","sessionId":"aaaabbbb-cccc-4ddd-8eee-ffff00001111",
                "batchId":batch["batchId"],"afterSeq":1}).to_string().into())).await.unwrap();
            // Wait for actual ACK persistence, then exercise the real 100ms
            // connected idle loop beyond its expired 500ms upload window.
            let mut tick = tokio::time::interval(Duration::from_millis(20));
            tokio::time::timeout(Duration::from_secs(1), async {
                loop {
                    tick.tick().await;
                    let m: Mirror = private_read(&persisted).unwrap();
                    if m.after_seq == 1 && m.pending.is_none() {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            let inode = fs::metadata(&persisted).unwrap().ino();
            for _ in 0..40 {
                tick.tick().await;
                assert_eq!(
                    fs::metadata(&persisted).unwrap().ino(),
                    inode,
                    "idle upload loop rewrote durable history"
                );
            }
            socket.close(None).await.unwrap();
        });
        let native = NativeSession {
            provider: "codex".into(),
            conversation: "native".into(),
            profile: "f".repeat(64),
            process: process(std::process::id()).unwrap(),
            transcript: None,
            socket: None,
            socket_inode: None,
            version: "0.149.0".into(),
            restriction: None,
            status: "unknown".into(),
            title: None,
            cwd_name: None,
            active_turn: None,
            last_assistant_message: None,
            holder: None,
            job: None,
            forked_from: None,
            forked_by_person: None,
            aliases: vec![],
            app: true,
            attention: Default::default(),
        };
        let mirror = Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native,
            generation: "g1".into(),
            previous_generation: None,
            session_id: None,
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![
                json!({"kind":"text","role":"assistant","text":"public output","truncated":false}),
            ],
            pending: None,
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            shared: true,
            stop_owed: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
            network_id: None,
        };
        let mut host = Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.path().into(),
            home: dir.path().into(),
            mirrors: BTreeMap::from([("local".into(), mirror)]),
            stop: Arc::new(AtomicBool::new(false)),
            hooks: None,
            rediscover: None,
            ended_pending: vec![],
            relay_reads_ended_start: None,
            engine: None,
            observed_at: None,
            discovery_read_ms: None,
            engine_missed: BTreeSet::new(),
            left_on_purpose: BTreeSet::new(),
            engine_woken: BTreeSet::new(),
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
            verify: verify_native,
            share_every: Some(true),
        };
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 1);
        assert!(stored.pending.is_none());
    }
    #[tokio::test]
    async fn local_sessions_a_successor_promoted_by_the_ack_registers_again() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["type"], "local.session.register");
            assert_eq!(first["session"]["generation"], "g1");
            // Hold the ACK back so the queued discovery pass -- the only other
            // ready arm -- is taken while this registration is in flight.
            tokio::time::sleep(Duration::from_millis(200)).await;
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,
                "generation":"g1","afterSeq":0,"serverTime":100,"reconcile":null})).await;
            let second = next_frame(&mut socket).await;
            assert_eq!(second["type"], "local.session.register");
            assert_ne!(
                second["session"]["generation"], "g1",
                "the generation the ACK promoted has never been registered"
            );
            assert_eq!(second["previousGeneration"], "g1");
            socket.close(None).await.unwrap();
        });
        let mut native = test_native("native");
        native.process.birth_micros += 1;
        let key = native_key(&native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), test_mirror("native", "g1"))]);
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        // The same session under a new process: discovery parks it as a
        // successor while registration is frozen.
        tx.send((vec![native], vec![], vec![], epoch_ms())).await.unwrap();
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        assert_ne!(host.mirrors[&key].generation, "g1");
        assert!(host.mirrors[&key].registration.successor.is_none());
    }

    #[tokio::test]
    async fn local_sessions_registration_ack_reconciles_a_lost_batch_ack() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            // The relay applied the 5-event batch whose ACK never reached this Mac.
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":383,"serverTime":100,"reconcile":null})).await;
            let next = tokio::time::timeout(Duration::from_millis(1500), socket.next()).await;
            assert!(next.is_err(), "an already-applied batch must not be re-sent: {next:?}");
            socket.close(None).await.unwrap();
        });
        let events: Vec<Value> = (0..5)
            .map(|i| json!({"kind":"text","role":"assistant","text":format!("line {i}"),"truncated":false}))
            .collect();
        let mut mirror = test_mirror("native", "g1");
        mirror.session_id = Some(SID.into());
        mirror.registration.acknowledged = Some("g1".into());
        mirror.after_seq = 378;
        mirror.queue = events.clone();
        mirror.pending = Some(json!({"type":"local.session.events","sessionId":SID,"generation":"g1",
            "batchId":"b1","afterSeq":378,"events":events}));
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 383);
        assert!(stored.pending.is_none());
        assert!(stored.queue.is_empty());
        assert_eq!(stored.registration.acknowledged.as_deref(), Some("g1"));
    }
    #[tokio::test]
    async fn local_sessions_irreconcilable_cursor_resyncs_and_mirrors_a_gap_marker() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let persisted = dir.path().join("local.json");
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            // Neither 378 nor 383: something else wrote this session's history.
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":400,"serverTime":100,"reconcile":null})).await;
            let batch = next_frame(&mut socket).await;
            assert_eq!(batch["type"], "local.session.events");
            assert_eq!(batch["afterSeq"], 400);
            assert_eq!(batch["events"].as_array().unwrap().len(), 1);
            assert_eq!(batch["events"][0]["role"], "system");
            assert!(batch["events"][0]["text"].as_str().unwrap().contains("could not mirror"));
            reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                "batchId":batch["batchId"],"afterSeq":401})).await;
            let mut tick = tokio::time::interval(Duration::from_millis(20));
            tokio::time::timeout(Duration::from_secs(1), async {
                loop {
                    tick.tick().await;
                    let m: Mirror = private_read(&persisted).unwrap();
                    if m.after_seq == 401 && m.pending.is_none() {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            socket.close(None).await.unwrap();
        });
        let events: Vec<Value> = (0..5)
            .map(|i| json!({"kind":"text","role":"assistant","text":format!("line {i}"),"truncated":false}))
            .collect();
        let mut mirror = test_mirror("native", "g1");
        mirror.session_id = Some(SID.into());
        mirror.registration.acknowledged = Some("g1".into());
        mirror.after_seq = 378;
        mirror.queue = events.clone();
        mirror.pending = Some(json!({"type":"local.session.events","sessionId":SID,"generation":"g1",
            "batchId":"b1","afterSeq":378,"events":events}));
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 401);
        assert!(stored.pending.is_none() && stored.queue.is_empty());
    }
    #[tokio::test]
    async fn local_sessions_one_rejected_session_does_not_block_the_others() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["session"]["generation"], "g-a", "keys register in order");
            // A generation this Mac never sent: a per-session verdict, not a broken peer.
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"other",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            let second = next_frame(&mut socket).await;
            assert_eq!(second["type"], "local.session.register");
            assert_eq!(second["session"]["generation"], "g-b", "the next session must still register on this connection");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID_B,"generation":"g-b",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            let quiet = tokio::time::timeout(Duration::from_millis(1200), socket.next()).await;
            assert!(quiet.is_err(), "the quarantined session must not be retried on this connection: {quiet:?}");
            socket.close(None).await.unwrap();
        });
        let mut host = test_host(dir.path(), vec![
            ("a-session", test_mirror("native-a", "g-a")),
            ("b-session", test_mirror("native-b", "g-b")),
        ]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let a: Mirror = private_read(&dir.path().join("a-session.json")).unwrap();
        let b: Mirror = private_read(&dir.path().join("b-session.json")).unwrap();
        assert!(a.session_id.is_none() && a.registration.acknowledged.is_none());
        assert_eq!(b.session_id.as_deref(), Some(SID_B));
    }
    #[tokio::test]
    async fn local_sessions_error_frame_for_the_inflight_registration_quarantines_only_that_session() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["session"]["generation"], "g-a");
            // Old backends send no sessionId; the in-flight RPC identifies the session.
            reply(&mut socket, json!({"type":"local.session.error","code":"LOCAL_SESSION_CONFLICT"})).await;
            let second = next_frame(&mut socket).await;
            assert_eq!(second["session"]["generation"], "g-b");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID_B,"generation":"g-b",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            socket.close(None).await.unwrap();
        });
        let mut host = test_host(dir.path(), vec![
            ("a-session", test_mirror("native-a", "g-a")),
            ("b-session", test_mirror("native-b", "g-b")),
        ]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let b: Mirror = private_read(&dir.path().join("b-session.json")).unwrap();
        assert_eq!(b.session_id.as_deref(), Some(SID_B));
    }
    #[tokio::test]
    async fn local_sessions_internal_relay_error_retries_the_inflight_registration_without_quarantine() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["session"]["generation"], "g-a");
            reply(&mut socket, json!({"type":"local.session.error","code":"INTERNAL"})).await;
            let retry = next_frame(&mut socket).await;
            assert_eq!(retry["session"]["generation"], "g-a", "the same session retries at once");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g-a",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            socket.close(None).await.unwrap();
        });
        let mut host = test_host(dir.path(), vec![("a-session", test_mirror("native-a", "g-a"))]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let a: Mirror = private_read(&dir.path().join("a-session.json")).unwrap();
        assert_eq!(a.session_id.as_deref(), Some(SID));
    }
    #[test]
    fn local_sessions_a_loop_that_was_not_scheduled_says_so() {
        let secs = Duration::from_secs;
        assert!(slow_iteration(Duration::from_millis(900), ("uploads", Duration::from_millis(800)), Duration::from_millis(100)).is_empty());
        // Slow work is named by its step; a short wait is not a scheduling problem.
        let busy = slow_iteration(secs(9), ("uploads", secs(8)), Duration::from_millis(100));
        assert_eq!(busy, ["connection loop iteration took 9.0s; slowest step: uploads (8.0s)"]);
        let napped = slow_iteration(secs(113), (WAIT, secs(112)), secs(112));
        assert_eq!(napped[0], "connection loop iteration took 113.0s; slowest step: waiting for an event (112.0s)");
        assert_eq!(napped[1], "connection loop not scheduled for 112.0s (App Nap or a starved runtime)");
        // The wait is its own lap: the arm that ran after it is charged only its own time.
        let mut laps = Laps::new();
        laps.lap = Instant::now() - secs(6);
        assert_eq!(laps.woke("tick"), "tick");
        assert!(laps.waited >= secs(6) && laps.slowest.0 == WAIT);
        laps.lap("tick");
        assert_eq!(laps.slowest.0, WAIT);
    }
    #[test]
    fn local_sessions_log_lines_start_with_their_utc_time() {
        assert_eq!(utc_stamp(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(utc_stamp(1_791_374_731_123), "2026-10-07T12:05:31.123Z");
        assert_eq!(utc_stamp(1_709_251_199_999), "2024-02-29T23:59:59.999Z");
    }

    #[tokio::test]
    async fn local_sessions_a_slow_reconciliation_never_delays_the_heartbeat_or_a_watch_nudge() {
        use std::sync::atomic::AtomicUsize;
        static CHECKS: AtomicUsize = AtomicUsize::new(0);
        static CHECKED: AtomicBool = AtomicBool::new(false);
        // The native check on a loaded Mac: `lsof` twice, `claude --version`, transcript reads.
        fn slow(_: PathBuf, _: NativeSession) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<NativeSession>> + Send>> {
            Box::pin(async {
                CHECKS.fetch_add(1, Ordering::SeqCst);
                tokio::time::sleep(Duration::from_millis(1500)).await;
                CHECKED.store(true, Ordering::SeqCst);
                Err("native identity changed".into())
            })
        }
        const WATCHED: &str = "dddd0000-1111-4222-8333-444455556666";
        const STUCK: &str = "request-stuck";
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror("native-slow", "gen-1");
        mirror.session_id = Some(WATCHED.into());
        // A send to a Claude session whose admission was never proven: `unknown` in the journal
        // and on the relay, which asks about it on every acknowledgment.
        let journal = Journal { dir: dir.path().join("receipts") };
        private_dir(&journal.dir).unwrap();
        let mut claude = test_native("native-slow");
        claude.provider = "claude".into();
        let stuck = Receipt { hash: None, generation: "gen-1".into(), outcome: "unknown".into(), provider_message_id: None,
            held: false, settled: false,
            ingress: Some(Ingress { native: claude, cursor: Cursor::default(), text: "hello".into(), workspace_revision: None }) };
        private_write(&journal.path(WATCHED, STUCK), &stuck).unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            assert_eq!(next_frame(&mut socket).await["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":WATCHED,"generation":"gen-1",
                "afterSeq":0,"serverTime":1000,"reconcile":null})).await;
            let ack = json!({"type":"local.device.heartbeat.ack","rejected":[],"serverTime":1001,
                "reconcile":[{"sessionId":WATCHED,"requestId":STUCK,"generation":"gen-1","state":"unknown"}]});
            let watch = json!({"type":"local.session.watch","sessionId":WATCHED,"generation":"gen-1"});
            assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
            reply(&mut socket, ack.clone()).await;
            reply(&mut socket, watch.clone()).await;
            let asked = Instant::now();
            // The next heartbeat is on time (its lease is 30 s; the stalled loop missed it by 80 s).
            assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
            assert!(asked.elapsed() < Duration::from_millis(700), "the heartbeat waited {:?} for the check", asked.elapsed());
            assert!(!CHECKED.load(Ordering::SeqCst), "the check had already finished: this proved nothing");
            assert!(super::super::screen::was_nudged(WATCHED), "the watch nudge waited for the check");
            // Every later ack asks again, as the relay did every 10 s; a viewer keeps arriving.
            let mut heartbeats = 1;
            let receipt = loop {
                reply(&mut socket, ack.clone()).await;
                reply(&mut socket, watch.clone()).await;
                let frame = next_frame(&mut socket).await;
                if frame["type"] == "local.session.receipt" {
                    break frame;
                }
                assert_eq!(frame["type"], "local.device.heartbeat");
                heartbeats += 1;
            };
            assert!(heartbeats >= 5, "only {heartbeats} heartbeats went out while the check ran");
            assert!(CHECKED.load(Ordering::SeqCst));
            assert_eq!((receipt["requestId"].as_str(), receipt["outcome"].as_str()), (Some(STUCK), Some("unknown")));
            // Still unknown: asked again at once, it is not checked again (not for a minute).
            for _ in 0..3 {
                assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
                reply(&mut socket, ack.clone()).await;
            }
            assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
            socket.close(None).await.unwrap();
        });
        let mut host = test_host(dir.path(), vec![("a-session", mirror)]);
        host.heartbeat_interval = Duration::from_millis(50);
        host.verify = slow;
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(10), host.connection(&mut socket, &mut rx, "alice")).await;
        server.await.unwrap();
        assert_eq!(CHECKS.load(Ordering::SeqCst), 1, "one check for one stuck command, however often it is asked");
    }

    #[test]
    fn local_sessions_an_unknown_reconciliation_is_checked_again_only_after_a_minute() {
        let (asks, mut asked) = tokio::sync::mpsc::unbounded_channel();
        let mut reconciles = Reconciles { asks, state: BTreeMap::new() };
        let ask = || Ask { session: SID.into(), request: "r1".into(), generation: "g".into(), turn: None,
            receipt: Receipt { hash: None, generation: "g".into(), outcome: "unknown".into(), provider_message_id: None,
                held: false, settled: false, ingress: None } };
        reconciles.ask(ask());
        reconciles.ask(ask());
        assert!(asked.try_recv().is_ok() && asked.try_recv().is_err(), "one check while one runs");
        reconciles.answered(&ask(), true);
        reconciles.ask(ask());
        assert!(asked.try_recv().is_err(), "unknown a moment ago: not checked again yet");
        reconciles.state.insert((SID.into(), "r1".into()), Some(Instant::now() - RECONCILE_UNKNOWN_EVERY));
        reconciles.ask(ask());
        assert!(asked.try_recv().is_ok(), "a minute later it is");
        reconciles.answered(&ask(), false);
        assert!(reconciles.state.is_empty(), "a settled command is forgotten");
    }

    #[tokio::test]
    async fn local_sessions_a_gateway_error_frame_does_not_end_the_connection() {
        // API Gateway's own frame when a backend call outlives the ws-message Lambda (10 s). It
        // has no `type`; taken as fatal, it tore the connection down (prod 2026-10-07).
        const WATCHED: &str = "7b0c1c0e-51f4-4a4e-9d0b-2f6a8f0f3d11";
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            assert_eq!(next_frame(&mut socket).await["type"], "local.session.register");
            // Someone watches this session: its own registration ack says so.
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g-a",
                "afterSeq":0,"serverTime":100,"reconcile":null,
                "watch":{"sessionId":WATCHED,"generation":"g-a","since":7}})).await;
            assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
            let gateway = json!({"message":"Internal server error","connectionId":"abc=","requestId":"def="});
            let ack = json!({"type":"local.device.heartbeat.ack","rejected":[],"reconcile":[],"serverTime":101});
            reply(&mut socket, gateway.clone()).await;
            // The Lambda gave up; the backend is still working and its ack still comes (prod
            // 2026-10-07: 10 s later). The request stays in flight: a second heartbeat sent now
            // would take the late ack as its own, and its own ack would then answer nothing.
            let quiet = tokio::time::timeout(Duration::from_millis(600), socket.next()).await;
            assert!(quiet.is_err(), "a second request went out while the first could still be answered");
            reply(&mut socket, ack.clone()).await;
            assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
            // An ack that answers nothing (its request was given up on) is ignored, like the
            // gateway's frame and a typed frame this app does not know.
            reply(&mut socket, ack.clone()).await;
            reply(&mut socket, ack).await;
            reply(&mut socket, json!({"type":"local.session.ended.ack"})).await;
            reply(&mut socket, gateway).await;
            reply(&mut socket, json!({"type":"local.session.from-a-newer-relay"})).await;
            assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
            socket.close(None).await.unwrap();
        });
        let mut host = test_host(dir.path(), vec![("a-session", test_mirror("native-a", "g-a"))]);
        host.heartbeat_interval = Duration::from_millis(200);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let ended = tokio::time::timeout(Duration::from_secs(5), host.connection(&mut socket, &mut rx, "alice")).await.unwrap();
        server.await.unwrap();
        assert_eq!(ended, Err("session socket closed".into()), "only the relay closing ends it");
        assert!(super::super::screen::was_nudged(WATCHED), "the watched session waited for a heartbeat ack");
    }
    #[tokio::test]
    async fn local_sessions_an_ack_later_than_the_gateways_ten_seconds_still_counts() {
        // Prod 2026-10-07 15:23:06Z: the backend took 15.6 s over one heartbeat. The Mac gave up
        // at 10 s and dropped the connection, so 39 sessions registered again and a teammate
        // who had just opened one saw nothing for 30 s. The ack was on its way.
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            assert_eq!(next_frame(&mut socket).await["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g-a",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
            tokio::time::sleep(Duration::from_millis(10_500)).await;
            reply(&mut socket, json!({"type":"local.device.heartbeat.ack","rejected":[],"reconcile":[],"serverTime":111})).await;
            assert_eq!(next_frame(&mut socket).await["type"], "local.device.heartbeat");
            socket.close(None).await.unwrap();
        });
        let mut host = test_host(dir.path(), vec![("a-session", test_mirror("native-a", "g-a"))]);
        host.heartbeat_interval = Duration::from_millis(50);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let ended = tokio::time::timeout(Duration::from_secs(20), host.connection(&mut socket, &mut rx, "alice")).await.unwrap();
        server.await.unwrap();
        assert_eq!(ended, Err("session socket closed".into()), "a slow backend is not a dead relay");
        assert!(ACK_DEADLINE >= Duration::from_secs(30), "the backend's worst seen answer is 15.6 s");
    }
    #[tokio::test]
    async fn local_sessions_rejected_batch_keeps_the_connection_and_the_pending_batch() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            let batch = next_frame(&mut socket).await;
            assert_eq!(batch["type"], "local.session.events");
            reply(&mut socket, json!({"type":"local.session.error","code":"LOCAL_SESSION_CONFLICT","sessionId":SID})).await;
            // Only a loop that is still polling socket.next() answers a ping; the old code
            // returned Err and never polled again, so it produced no pong.
            socket.send(Message::Ping(b"still-there".to_vec().into())).await.unwrap();
            let pong = tokio::time::timeout(Duration::from_millis(1500), socket.next()).await;
            assert!(matches!(pong, Ok(Some(Ok(Message::Pong(_))))), "a rejected batch must not drop the connection: {pong:?}");
            socket.close(None).await.unwrap();
        });
        let mut mirror = test_mirror("native", "g1");
        mirror.queue = vec![json!({"kind":"text","role":"assistant","text":"public output","truncated":false})];
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert!(stored.pending.is_some(), "the rejected batch stays pending for the next connection's reconciliation");
        assert_eq!(stored.after_seq, 0);
    }
    #[tokio::test]
    async fn local_sessions_rate_limited_batch_retries_on_the_same_connection() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            let batch = next_frame(&mut socket).await;
            assert_eq!(batch["type"], "local.session.events");
            // Retryable by contract (retry_after 0.5s), so it must not darken the session.
            reply(&mut socket, json!({"type":"local.session.error","code":"RATE_LIMITED","sessionId":SID})).await;
            let frame = tokio::time::timeout(Duration::from_millis(3500), socket.next())
                .await
                .expect("the throttled batch retries on this connection")
                .expect("stream open")
                .expect("frame ok");
            let retry: Value = serde_json::from_str(frame.to_text().unwrap()).unwrap();
            assert_eq!(retry["type"], "local.session.events");
            assert_eq!(retry["batchId"], batch["batchId"], "the same pending batch retries");
            reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                "batchId":retry["batchId"],"afterSeq":1})).await;
            socket.close(None).await.unwrap();
        });
        let mut mirror = test_mirror("native", "g1");
        mirror.queue = vec![json!({"kind":"text","role":"assistant","text":"public output","truncated":false})];
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 1);
        assert!(stored.pending.is_none());
    }
    #[tokio::test]
    async fn local_sessions_stale_duplicate_upload_ack_does_not_drop_the_connection() {
        // Reproduces the prod incident of 2026-09-12: B1 goes unacked past the 2s
        // resend window and is re-sent; the relay's real ack for B1 arrives and the
        // desktop moves on to B2; then the relay's OWN duplicate ack for B1 (its
        // second reply, to the resend) arrives while B2 is the outstanding pending
        // batch. The old code compared the duplicate's batchId against the now-different
        // pending batch and treated it as an unrecoverable conflict, tearing the
        // connection down. It must instead be recognised as an already-applied stale
        // ack and ignored.
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;

            // B1: the oversized first event alone -- the second event cannot fit
            // alongside it, so next_batch is forced to split them into B1 and B2.
            let batch1 = next_frame(&mut socket).await;
            assert_eq!(batch1["type"], "local.session.events");
            assert_eq!(batch1["events"].as_array().unwrap().len(), 1);

            // Withhold the ack past the 2s pending-resend window.
            let frame = tokio::time::timeout(Duration::from_millis(3500), socket.next())
                .await
                .expect("B1 retries within 3.5s")
                .expect("stream open")
                .expect("frame ok");
            let retry: Value = serde_json::from_str(frame.to_text().unwrap()).unwrap();
            assert_eq!(retry["type"], "local.session.events");
            assert_eq!(retry["batchId"], batch1["batchId"], "the same pending batch retries");

            // Ack B1 for real: cursor advances, pending clears, and the remaining
            // queued event becomes the next batch (B2).
            reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                "batchId":batch1["batchId"],"afterSeq":1})).await;
            let batch2 = next_frame(&mut socket).await;
            assert_eq!(batch2["type"], "local.session.events");
            assert_ne!(batch2["batchId"], batch1["batchId"]);
            assert_eq!(batch2["events"][0]["text"], "second event");

            // The relay's second reply to the RESENT B1 arrives now: a duplicate ack
            // for an already-applied batch, while B2 is genuinely outstanding.
            reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                "batchId":batch1["batchId"],"afterSeq":1})).await;

            // Only a loop that is still polling socket.next() answers a ping; the old
            // code returned Err out of connection() and never polled again.
            socket.send(Message::Ping(b"still-there".to_vec().into())).await.unwrap();
            let pong = tokio::time::timeout(Duration::from_millis(1500), socket.next()).await;
            assert!(matches!(pong, Ok(Some(Ok(Message::Pong(_))))),
                "a stale duplicate ack for an already-applied batch must not drop the connection: {pong:?}");
            socket.close(None).await.unwrap();
            batch2["batchId"].as_str().unwrap().to_owned()
        });

        // Build a first event just under the 16 KiB envelope budget so the second
        // event can never join it in the same batch.
        let placeholder_id = "00000000-0000-4000-8000-000000000000";
        let envelope_len = |events: &[Value]| {
            ascii_wire_len(&json!({"type":"local.session.events","sessionId":SID,"generation":"g1",
                "batchId":placeholder_id,"afterSeq":0,"events":events}))
                .unwrap()
        };
        let base_len = envelope_len(&[json!({"kind":"text","role":"assistant","text":"","truncated":false})]);
        let big_event = json!({"kind":"text","role":"assistant","text":"x".repeat(16384 - base_len - 5),"truncated":false});
        let second_event = json!({"kind":"text","role":"assistant","text":"second event","truncated":false});
        assert!(envelope_len(&[big_event.clone()]) <= 16384, "test setup: the first event alone must fit");
        assert!(envelope_len(&[big_event.clone(), second_event.clone()]) > 16384,
            "test setup: both events together must overflow one batch");

        let mut mirror = test_mirror("native", "g1");
        mirror.queue = vec![big_event, second_event];
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        let batch2_id = server.await.unwrap();

        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 1, "B1's real ack must still apply");
        let pending = stored.pending.expect("B2 remains outstanding, untouched by the stale duplicate");
        assert_eq!(pending["batchId"], batch2_id);
    }
    #[tokio::test]
    async fn local_sessions_named_error_without_an_outstanding_batch_frees_the_rpc_slot() {
        let dir = tempfile::tempdir().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["session"]["generation"], "g-a");
            // A restored session already carries its id, so this rejection is a NAMED error with
            // no batch outstanding -- the shape of a rejected heartbeat or receipt, whose reply
            // this error is. Leaving the RPC slot occupied would stall until the ack deadline.
            reply(&mut socket, json!({"type":"local.session.error","code":"LOCAL_SESSION_CONFLICT","sessionId":SID})).await;
            let second = next_frame(&mut socket).await;
            assert_eq!(second["type"], "local.session.register");
            assert_eq!(second["session"]["generation"], "g-b", "the RPC slot must be free for the next session");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID_B,"generation":"g-b",
                "afterSeq":0,"serverTime":100,"reconcile":null})).await;
            socket.close(None).await.unwrap();
        });
        let mut a = test_mirror("native-a", "g-a");
        a.session_id = Some(SID.into());
        let mut host = test_host(dir.path(), vec![("a-session", a), ("b-session", test_mirror("native-b", "g-b"))]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let b: Mirror = private_read(&dir.path().join("b-session.json")).unwrap();
        assert_eq!(b.session_id.as_deref(), Some(SID_B));
    }
    #[test]
    fn local_sessions_quarantine_is_time_bound_not_connection_bound() {
        let mut quarantined = BTreeMap::new();
        assert!(!quarantine_active(&quarantined, "absent"));
        quarantined.insert("fresh".to_string(), Instant::now());
        assert!(quarantine_active(&quarantined, "fresh"));
        if let Some(expired) = Instant::now().checked_sub(QUARANTINE + Duration::from_secs(1)) {
            quarantined.insert("stale".to_string(), expired);
            assert!(!quarantine_active(&quarantined, "stale"));
        }
    }
    #[tokio::test]
    async fn local_sessions_an_answer_already_buffered_wins_over_an_overslept_deadline() {
        // The loop was busy past the deadline; the answer arrived before it.
        let overslept = Some(Instant::now() - Duration::from_secs(1));
        let mut answered = futures_util::stream::iter(["ack"]);
        assert_eq!(next_or_deadline(&mut answered, overslept).await, Ok(Some("ack")));
        let mut silent = futures_util::stream::pending::<&str>();
        assert!(next_or_deadline(&mut silent, overslept).await.is_err());
        // Nothing in flight: no deadline at all.
        let idle = tokio::time::timeout(Duration::from_millis(50), next_or_deadline(&mut silent, None));
        assert!(idle.await.is_err());
    }
    #[test]
    fn local_sessions_a_late_acknowledgment_counts_but_keeps_the_previous_clock_anchor() {
        let server = epoch_secs();
        let late = ack_anchor(
            server,
            Instant::now() - Duration::from_secs(11),
            SystemTime::now() - Duration::from_secs(11),
        );
        assert!(late.is_none(), "a late ACK must not move the anchor");
        let prompt = ack_anchor(server, Instant::now(), SystemTime::now()).expect("a prompt ACK anchors");
        assert_eq!(prompt.server, server);
    }
    #[test]
    fn local_sessions_reconnect_delay_backs_off_unless_the_connection_lived() {
        assert_eq!(reconnect_delay(1, Duration::from_secs(0)), 2);
        assert_eq!(reconnect_delay(2, Duration::from_secs(5)), 4);
        assert_eq!(reconnect_delay(16, Duration::from_secs(29)), 30);
        assert_eq!(reconnect_delay(30, Duration::from_secs(1)), 30);
        assert_eq!(reconnect_delay(30, Duration::from_secs(30)), 1);
        assert_eq!(reconnect_delay(4, Duration::from_secs(600)), 1);
    }
    #[test]
    fn local_sessions_restart_delay_backs_off_unless_run_once_lived_a_minute() {
        assert_eq!(restart_delay(1, Duration::from_secs(0)), 2);
        assert_eq!(restart_delay(2, Duration::from_secs(59)), 4);
        assert_eq!(restart_delay(30, Duration::from_secs(60)), 1);
        assert_eq!(restart_delay(40, Duration::from_secs(10)), 60);
    }
    #[tokio::test]
    async fn local_sessions_supervisor_keeps_running_after_the_network_loop_ends_and_honours_stop() {
        let dir = tempfile::tempdir().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let host = Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            // Nonexistent: the first discovery-driven observe()/persist fails here, so
            // run_once returns on its own (not via `stop`) -- the case that actually
            // exercises the restart loop, unlike an always-blocking empty-mirrors host.
            dir: dir.path().join("missing"),
            home: dir.path().into(),
            mirrors: BTreeMap::from([("native".to_string(), test_mirror("native", "g1"))]),
            stop: stop.clone(),
            hooks: None,
            rediscover: None,
            ended_pending: vec![],
            relay_reads_ended_start: None,
            engine: None,
            observed_at: None,
            discovery_read_ms: None,
            engine_missed: BTreeSet::new(),
            left_on_purpose: BTreeSet::new(),
            engine_woken: BTreeSet::new(),
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
            verify: verify_native,
            share_every: Some(true),
        };
        let enrollment = Enrollment {
            owner_id: "org".into(),
            human_user_id: "alice".into(),
            device_id: "mac".into(),
            gateway_url: "wss://127.0.0.1:1".into(),
            enabled: true,
            paused: false,
            server_revision: None,
            remote_disabled: false,
            attempt: None,
        };
        let identity = crate::keychain::generate_keypair();
        let status = Arc::new(std::sync::Mutex::new(String::new()));
        let wake = Arc::new(tokio::sync::Notify::new());
        let stopper = stop.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(2500)).await;
            stopper.store(true, Ordering::SeqCst);
        });
        let started = Instant::now();
        let (_hooks_tx, hooks_rx) = tokio::sync::mpsc::channel(1);
        tokio::time::timeout(
            Duration::from_secs(8),
            host.supervise(enrollment, identity, wake, status.clone(), hooks_rx),
        )
        .await
        .expect("supervise returns once stop is set");
        // A single non-looping run_once call would have returned as soon as the very
        // first attempt errored -- milliseconds in, well under the stop flip at 2.5s.
        // Only a loop that restarts run_once and waits for `stop` takes at least that long.
        assert!(
            started.elapsed() >= Duration::from_millis(2500),
            "returned too fast for a restart loop: {:?}",
            started.elapsed()
        );
        assert_eq!(status.lock().unwrap().as_str(), "disconnected");
    }
    #[tokio::test]
    async fn local_sessions_registration_ack_with_batchless_pending_falls_through_to_resync() {
        let dir = tempfile::tempdir().unwrap();
        let persisted = dir.path().join("local.json");
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            // Cursor math says "the pending batch applied" (after == after_seq + pending_count),
            // but the persisted pending row has no batchId to acknowledge against.
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,"generation":"g1",
                "afterSeq":379,"serverTime":100,"reconcile":null})).await;
            let batch = next_frame(&mut socket).await;
            assert_eq!(batch["type"], "local.session.events");
            assert_eq!(batch["afterSeq"], 379);
            assert_eq!(batch["events"].as_array().unwrap().len(), 1);
            assert_eq!(batch["events"][0]["role"], "system");
            assert!(batch["events"][0]["text"].as_str().unwrap().contains("could not mirror"));
            reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                "batchId":batch["batchId"],"afterSeq":380})).await;
            let mut tick = tokio::time::interval(Duration::from_millis(20));
            tokio::time::timeout(Duration::from_secs(1), async {
                loop {
                    tick.tick().await;
                    let m: Mirror = private_read(&persisted).unwrap();
                    if m.after_seq == 380 && m.pending.is_none() {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            socket.close(None).await.unwrap();
        });
        let events: Vec<Value> = vec![json!({"kind":"text","role":"assistant","text":"line 0","truncated":false})];
        let mut mirror = test_mirror("native", "g1");
        mirror.session_id = Some(SID.into());
        mirror.registration.acknowledged = Some("g1".into());
        mirror.after_seq = 378;
        mirror.queue = events.clone();
        // No "batchId" field at all -- the buggy path treated this as `unwrap_or("")`.
        mirror.pending = Some(json!({"type":"local.session.events","sessionId":SID,"generation":"g1",
            "afterSeq":378,"events":events}));
        let mut host = test_host(dir.path(), vec![("local", mirror)]);
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}")).await.unwrap();
        let _ = host.connection(&mut socket, &mut rx, "alice").await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 380);
        assert!(stored.pending.is_none() && stored.queue.is_empty());
    }

    const GOLDEN_HEARTBEAT: &str = include_str!(
        "../../../../../packages/shared/fixtures/local-session-frames/device-heartbeat.json"
    );
    const GOLDEN_HEARTBEAT_ACK: &str = include_str!(
        "../../../../../packages/shared/fixtures/local-session-frames/device-heartbeat-ack.json"
    );
    const GOLDEN_ENDED: &str =
        include_str!("../../../../../packages/shared/fixtures/local-session-frames/session-ended.json");
    const GOLDEN_ENDED_ACK: &str = include_str!(
        "../../../../../packages/shared/fixtures/local-session-frames/session-ended-ack.json"
    );

    #[test]
    fn local_sessions_idle_heartbeat_is_the_golden_frame_with_no_sessions() {
        let golden: Value = serde_json::from_str(GOLDEN_HEARTBEAT).unwrap();
        let idle = idle_heartbeat();
        // The golden frame also carries the live-terminal features; an idle Mac has nothing to
        // watch, and its relay features are unknown until a registration ACK.
        assert!(frame_keys(&idle).is_subset(&frame_keys(&golden)));
        assert_eq!(frame_keys(&idle), BTreeSet::from(["type", "sessions"]));
        assert_eq!(idle["sessions"], json!([]));
    }

    fn frame_keys(value: &Value) -> BTreeSet<&str> {
        value
            .as_object()
            .expect("frame object")
            .keys()
            .map(String::as_str)
            .collect()
    }

    fn engine_row(status: &str) -> EngineRow {
        let me = process(std::process::id()).unwrap();
        EngineRow {
            key: format!("abc/claude:{SID}"), provider: "claude".into(), conversation: SID.into(), pid: me.pid, birth_micros: me.birth_micros,
            cwd: "/x".into(), status: engine_view::desktop_status(status).into(), waiting_for: None, kind: None, title: None, job: None,
        }
    }

    fn engine_ended(reason: Option<&str>) -> EngineEnded {
        EngineEnded { key: format!("abc/claude:{SID}"), provider: "claude".into(), conversation: SID.into(), reason: reason.map(Into::into) }
    }

    fn engine_live(rows: Vec<EngineRow>) -> Result<EngineView> {
        Ok(EngineView { live: rows, ended: vec![] })
    }

    fn engine_host(dir: &Path) -> Host {
        let mut mirror = test_mirror(SID, "g");
        mirror.native.provider = "claude".into();
        test_host(dir, vec![(SID, mirror)])
    }

    /// `claude agents --json` as it answers now, and how long it took.
    async fn claude_agents(program: &Path) -> (Vec<serde_json::Value>, Duration) {
        let at = Instant::now();
        let out = tokio::time::timeout(Duration::from_secs(20), tokio::process::Command::new(program).args(["agents", "--json"]).kill_on_drop(true).output())
            .await
            .expect("claude agents answers within 20 s")
            .unwrap();
        (serde_json::from_slice(&out.stdout).expect("claude agents says JSON"), at.elapsed())
    }

    /// Why this mirror's idle is not backed by Claude's own listing (A5), if it is not.
    fn idle_unbacked(mirrors: &BTreeMap<String, Mirror>, agents: &[serde_json::Value]) -> Vec<(String, String)> {
        let idle = mirrors.values().filter(|m| m.ended_at.is_none() && m.native.status == "idle");
        idle.filter_map(|m| {
            let conversation = &m.native.conversation;
            match agents.iter().find(|a| a["sessionId"] == conversation.as_str()) {
                None => Some((conversation.clone(), "absent".to_owned())),
                Some(a) if a["status"] == "idle" => None,
                Some(a) => Some((conversation.clone(), format!("status {}", a["status"]))),
            }
        })
        .collect()
    }

    /// The next engine answer that satisfies `until`, within a minute: a `claude` read and an end
    /// each land on the engine's own ticks.
    async fn engine_until(what: &str, until: impl Fn(&EngineView) -> bool) -> EngineView {
        for _ in 0..120 {
            let view = engine_view::read().await.unwrap();
            if until(&view) {
                return view;
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
        panic!("the engine never said: {what}");
    }

    /// Run against an isolated 0.8.0 engine only (the plan's Verification says how); the session
    /// rows it registers would otherwise land on the person's own service. `$HOME` is read only,
    /// the Host's state is a tempdir, and no Host here has a relay.
    #[tokio::test(flavor = "multi_thread")]
    #[ignore = "needs an isolated 0.8.0 engine at LIGHTBULB_CONNECT_HOME"]
    async fn real_engine_drives_mirrors() {
        let engine_home = std::env::var("LIGHTBULB_CONNECT_HOME").expect("set LIGHTBULB_CONNECT_HOME to an isolated engine");
        let home = PathBuf::from(std::env::var_os("HOME").unwrap());
        let real = |path: &Path| path.canonicalize().unwrap_or_else(|_| path.to_owned());
        assert_ne!(real(Path::new(&engine_home)), real(&home.join(".lightbulb/connect")), "never the person's own service");
        let state = tempfile::tempdir().unwrap();
        let mut host = test_host(state.path(), vec![]);
        host.home = home.clone();
        super::super::session_states::set_reads_network_id(true);

        let at = Instant::now();
        let view = engine_view::read().await.expect("the real answer parses");
        println!("sessions: {} live, {} ended, in {:?} (the desktop gives it 5 s)", view.live.len(), view.ended.len(), at.elapsed());
        // One answer read two ways: no row may fall out of the parse.
        let raw = crate::engine_client::ask("sessions", None).await.unwrap();
        let rows = raw["sessions"].as_array().unwrap();
        assert_eq!(engine_view::parse_sessions(&raw).unwrap().live.len(), rows.len(), "rows the parse dropped, from: {rows:#?}");

        let found = claude::discover(&home).await.unwrap();
        println!("discover: {} sessions, {} jobs", found.sessions.len(), found.jobs.len());
        host.observe(found.sessions).unwrap();
        let view = engine_view::read().await.unwrap();
        host.apply_engine(&Ok(view));
        assert!(!host.mirrors.is_empty(), "this Mac has live Claude sessions; none became a mirror");

        let program = crate::local_sessions::provider_program(&home, "claude").unwrap();
        let (mut agents, took) = claude_agents(&program).await;
        let mut unbacked = idle_unbacked(&host.mirrors, &agents);
        if !unbacked.is_empty() {
            // Machine load flips busy and idle mid-read.
            println!("first read disagrees: {unbacked:?}; reading both again");
            tokio::time::sleep(Duration::from_secs(5)).await;
            host.apply_engine(&Ok(engine_view::read().await.unwrap()));
            (agents, _) = claude_agents(&program).await;
            unbacked = idle_unbacked(&host.mirrors, &agents);
        }
        let view = engine_view::read().await.unwrap();
        println!("{:<38} {:<18} {:<18} claude agents ({took:?})", "conversation", "mirror", "engine");
        for m in host.mirrors.values() {
            let conversation = &m.native.conversation;
            let engine = view.live.iter().find(|r| r.conversation == *conversation).map_or("no live row", |r| r.status.as_str());
            let claude = agents.iter().find(|a| a["sessionId"] == conversation.as_str()).map_or("absent".to_owned(), |a| a["status"].to_string());
            println!("{conversation:<38} {:<18} {engine:<18} {claude}", m.native.status);
        }
        // The engine logs a failed read once per run of them, in a log that outlasts its starts
        // (each says `relay:`). Absent passes only on that word from this start.
        let log = fs::read_to_string(Path::new(&engine_home).join("daemon.log")).unwrap_or_default();
        let timed_out = log.rsplit("relay: ").next().is_some_and(|run| run.contains("claude agents: no answer"));
        println!("A5: idle mirrors not backed by claude agents: {unbacked:?}; the engine's listing read timed out since it started: {timed_out}");
        assert!(unbacked.iter().all(|(_, why)| timed_out && why == "absent"), "A5: {unbacked:?}");

        for m in host.mirrors.values() {
            let Some(row) = view.live.iter().find(|r| r.provider == m.native.provider && r.conversation == m.native.conversation) else {
                println!("{} left the engine's live rows during this run", m.native.conversation);
                continue;
            };
            assert_eq!(m.network_id.as_deref(), Some(row.key.as_str()));
            let frame = registration(m);
            assert_eq!(frame["session"]["networkSessionId"].as_str(), Some(row.key.as_str()));
        }
        println!("network_id: each mirror's is its engine row's key, and its registration frame carries it");

        // The end path, on processes the engine lists without a Claude. An idle row that Claude's
        // listing does not name goes after 15 s (`moved_on`), while a `waiting` one lives with its
        // process, so each real reason gets its own row.
        let cwd = tempfile::tempdir().unwrap();
        let register = async |status: &str| {
            let child = tokio::process::Command::new("/bin/sleep").arg("600").kill_on_drop(true).spawn().unwrap();
            let me = process(child.id().unwrap()).unwrap();
            let conversation = uuid::Uuid::new_v4().to_string();
            let session = json!({"provider":"claude","conversation":conversation,"pid":me.pid,"birth_micros":me.birth_micros,
                "cwd":cwd.path().to_str().unwrap(),"status":status,"waiting_for":"permission prompt"});
            let answer = crate::engine_client::ask("register", Some(json!({ "session": session }))).await.unwrap();
            println!("register {status}: {answer}");
            assert_eq!(answer["ok"], true);
            (child, conversation)
        };
        let (mut waiting, gone_id) = register("waiting").await;
        let (idle, left_id) = register("idle").await;
        let view = engine_until("both sleeps are live", |v| [&gone_id, &left_id].iter().all(|id| v.live.iter().any(|r| r.conversation == **id))).await;
        for id in [&gone_id, &left_id] {
            let row = view.live.iter().find(|r| r.conversation == *id).unwrap();
            println!("live: {} status={} waiting_for={:?} kind={:?} pid={}", row.key, row.status, row.waiting_for, row.kind, row.pid);
        }
        let found = claude::discover(&home).await.unwrap();
        host.observe(found.sessions).unwrap();
        host.apply_engine(&Ok(engine_view::read().await.unwrap()));
        let key_of = |host: &Host, id: &str| host.mirrors.iter().find(|(_, m)| m.native.conversation == id).map(|(k, _)| k.clone()).expect("the real discover made a mirror of this row");
        let (gone_key, left_key) = (key_of(&host, &gone_id), key_of(&host, &left_id));
        let m = &host.mirrors[&gone_key];
        println!("mirror: status={} restriction={:?} network_id={:?}", m.native.status, m.native.restriction, m.network_id);
        assert_eq!(m.native.status, "waiting-approval");

        waiting.kill().await.unwrap();
        let ends = async |host: &mut Host, id: &str, key: &str, want: &str| {
            let view = engine_until("it ended", |v| v.ended.iter().any(|r| r.conversation == id) && v.live.iter().all(|r| r.conversation != id)).await;
            let reason = view.ended.iter().find(|r| r.conversation == id).unwrap().reason.clone();
            host.apply_engine(&Ok(view));
            println!("real ended row: reason={reason:?}; first miss: ended_at={:?}", host.mirrors[key].ended_at);
            assert!(host.mirrors[key].ended_at.is_none(), "one answer ends nothing");
            host.apply_engine(&Ok(engine_view::read().await.unwrap()));
            println!("second miss: ended_reason={:?}", host.mirrors[key].ended_reason);
            assert_eq!(host.mirrors[key].ended_reason.as_deref(), Some(want));
        };
        ends(&mut host, &gone_id, &gone_key, "process-exited").await;
        ends(&mut host, &left_id, &left_key, "exited").await;
        drop(idle);
    }

    fn held_prompt(reply: &str) -> WaitingPrompt {
        WaitingPrompt {
            id: None, tool: None, summary: String::new(),
            held: Some(approval::Held { id: "appr-1".into(), input_digest: "d".repeat(64), reply: reply.into(), exact: true, modes: vec![], full: false, similar: None }),
            private_send: None, input: None, shipped_full: false,
        }
    }

    #[test]
    fn local_sessions_engine_row_sets_a_claude_mirrors_state() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        host.mirrors.get_mut(SID).unwrap().native.attention.note = Some("press Resume".into());
        for (engine, want) in [("idle", "idle"), ("live", "busy")] {
            host.apply_engine(&engine_live(vec![engine_row(engine)]));
            assert_eq!(host.mirrors[SID].native.status, want);
            assert!(host.heartbeat_dirty.remove(SID), "{engine}: the relay hears soon");
        }
        host.apply_engine(&engine_live(vec![engine_row("live")]));
        assert!(host.heartbeat_dirty.is_empty(), "nothing moved");
        let mut waiting = engine_row("waiting");
        waiting.waiting_for = Some("permission prompt".into());
        host.apply_engine(&engine_live(vec![waiting]));
        assert_eq!(host.mirrors[SID].native.status, "waiting-approval");
        assert_eq!(host.mirrors[SID].native.attention.waiting_for.as_deref(), Some("permission prompt"));
        let stored: Mirror = private_read(&dir.path().join(format!("{SID}.json"))).unwrap();
        assert_eq!(stored.native.status, "waiting-approval", "a change persists");
        let mut finished = engine_row("live");
        finished.kind = Some("background".into());
        finished.job = Some(("deadbeef".into(), "done".into()));
        host.apply_engine(&engine_live(vec![finished]));
        let native = &host.mirrors[SID].native;
        assert_eq!((native.status.as_str(), native.attention.done, native.attention.waiting_for.as_deref(), native.attention.note.as_deref()), ("idle", true, None, Some("press Resume")));
        assert!(host.mirrors[SID].ended_at.is_none());
    }

    #[test]
    fn local_sessions_engine_ends_a_claude_mirror_with_its_reason() {
        for (ended, want) in [
            (Some("moved-on"), "exited"),
            (Some("session-end"), "exited"),
            (Some("process-exited"), "process-exited"),
            (None, "process-exited"),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let mut host = engine_host(dir.path());
            let answer = Ok(EngineView { live: vec![], ended: ended.map(|r| engine_ended(Some(r))).into_iter().collect() });
            host.apply_engine(&answer);
            assert!(host.mirrors[SID].ended_at.is_none() && host.ended_pending.is_empty(), "{ended:?}: one miss is no end");
            host.apply_engine(&answer);
            assert_eq!(host.mirrors[SID].ended_reason.as_deref(), Some(want), "{ended:?}");
            assert_eq!(host.ended_pending, vec![SID.to_owned()]);
        }
    }

    #[test]
    fn local_sessions_a_claude_hook_says_on_purpose_and_the_engine_ends_it() {
        for (reason, want) in [("prompt_input_exit", "on-purpose"), ("other", "exited")] {
            let dir = tempfile::tempdir().unwrap();
            let mut host = engine_host(dir.path());
            let mut end = hook(lifecycle::Provider::Claude, "SessionEnd", SID);
            end.reason = Some(reason.into());
            host.apply_hook(&end).unwrap();
            host.apply_engine(&engine_live(vec![engine_row("idle")]));
            assert!(host.mirrors[SID].ended_at.is_none() && host.ended_pending.is_empty(), "the hook alone ends nothing");
            for _ in 0..2 {
                host.apply_engine(&Ok(EngineView { live: vec![], ended: vec![engine_ended(Some("session-end"))] }));
            }
            assert_eq!(host.mirrors[SID].ended_reason.as_deref(), Some(want));
            assert!(host.left_on_purpose.is_empty());
        }
    }

    #[test]
    fn local_sessions_hooks_leave_a_claude_status_alone_and_keep_the_rest() {
        for provider in [lifecycle::Provider::Claude, lifecycle::Provider::Codex] {
            let claude = provider == lifecycle::Provider::Claude;
            for (name, notification, moved) in [("UserPromptSubmit", None, "busy"), ("Stop", None, "idle"), ("Notification", Some("permission_prompt"), "waiting-approval")] {
                let dir = tempfile::tempdir().unwrap();
                let mut mirror = test_mirror(SID, "g");
                mirror.native.provider = if claude { "claude" } else { "codex" }.into();
                mirror.native.status = "unknown".into();
                mirror.waiting = Some(WaitingPrompt { held: None, ..held_prompt("") });
                let mut host = test_host(dir.path(), vec![(SID, mirror)]);
                let mut event = hook(provider.clone(), name, SID);
                event.notification_type = notification.map(Into::into);
                event.last_assistant_message = Some("said".into());
                host.apply_hook(&event).unwrap();
                let m = &host.mirrors[SID];
                assert_eq!(m.native.status, if claude { "unknown" } else { moved }, "{name}");
                assert!(m.waiting.is_none(), "{name}: an open un-held prompt is over");
                assert_eq!(m.native.last_assistant_message.as_deref(), Some("said"));
            }
            // A prompt nothing holds: the row is recorded, the status is the engine's.
            let dir = tempfile::tempdir().unwrap();
            let mut mirror = test_mirror(SID, "g");
            mirror.native.provider = if claude { "claude" } else { "codex" }.into();
            let mut host = test_host(dir.path(), vec![(SID, mirror)]);
            host.apply_hook(&hook(provider.clone(), "PermissionRequest", SID)).unwrap();
            let m = &host.mirrors[SID];
            assert_eq!(m.native.status, if claude { "unknown" } else { "waiting-approval" });
            assert!(m.waiting.is_some() && m.queue.len() == 1);
        }
    }

    #[test]
    fn local_sessions_a_held_prompt_outlives_the_engines_busy_until_released() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        host.mirrors.get_mut(SID).unwrap().waiting = Some(held_prompt(""));
        host.apply_engine(&engine_live(vec![engine_row("live")]));
        assert_eq!(host.mirrors[SID].native.status, "waiting-approval");
        host.apply_engine(&Err("down".into()));
        assert_eq!(host.mirrors[SID].native.status, "waiting-approval", "the hold outlives a dead engine too");
        let mut release = hook(lifecycle::Provider::Claude, "PermissionRelease", SID);
        release.held = held_prompt("").held;
        assert_eq!(host.apply_hook(&release).unwrap(), Some(SID.to_owned()));
        host.apply_engine(&engine_live(vec![engine_row("live")]));
        assert_eq!(host.mirrors[SID].native.status, "busy");
        // Codex's daemon hold is not this rule's.
        host.mirrors.get_mut(SID).unwrap().waiting = Some(held_prompt(CODEX_DAEMON));
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        assert_eq!(host.mirrors[SID].native.status, "idle");
    }

    #[test]
    fn local_sessions_an_engine_fault_ends_only_a_dead_pid_and_reads_unknown() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        host.mirrors.get_mut(SID).unwrap().native.status = "busy".into();
        let mut dead = test_mirror(SID_B, "g");
        dead.native.provider = "claude".into();
        dead.native.process.pid = 2_147_483_000;
        host.mirrors.insert(SID_B.into(), dead);
        let mut restarted = test_mirror("restarted", "g");
        restarted.native.provider = "claude".into();
        restarted.native.process.pid = 2_147_483_000;
        restarted.registration.successor = Some(test_native("restarted"));
        host.mirrors.insert("restarted".into(), restarted);
        let mut codex = test_mirror("codex", "g");
        codex.native.status = "busy".into();
        codex.native.process.pid = 2_147_483_000;
        host.mirrors.insert("codex".into(), codex);
        host.apply_engine(&Err("down".into()));
        assert!(host.mirrors[SID].ended_at.is_none());
        assert_eq!(host.mirrors[SID].native.status, "unknown");
        assert!(host.heartbeat_dirty.contains(SID));
        assert_eq!(host.mirrors[SID_B].ended_reason.as_deref(), Some("process-exited"));
        assert!(host.mirrors["restarted"].ended_at.is_none(), "its successor runs");
        assert!(host.mirrors["codex"].ended_at.is_none() && host.mirrors["codex"].native.status == "busy", "only Claude's");
    }

    #[test]
    fn local_sessions_the_poller_says_a_fault_only_after_three_failures() {
        let mut failures = 0;
        let failed = |failures: &mut u32| engine_answer(failures, Err("down".into()));
        assert!(failed(&mut failures).is_none());
        assert!(failed(&mut failures).is_none());
        assert!(failed(&mut failures).is_some_and(|answer| answer.is_err()));
        assert!(failed(&mut failures).is_some_and(|answer| answer.is_err()), "and goes on saying it");
        assert!(engine_answer(&mut failures, engine_live(vec![])).is_some_and(|answer| answer.is_ok()));
        assert!(failed(&mut failures).is_none(), "one failure after an answer is nothing");
    }

    #[test]
    fn local_sessions_a_row_with_no_mirror_wakes_discovery_once() {
        use futures_util::FutureExt;
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![]);
        let wake = Arc::new(tokio::sync::Notify::new());
        host.rediscover = Some(wake.clone());
        let woke = || wake.notified().now_or_never().is_some();
        // A row whose capture always fails: discovery runs for it once, not every poll.
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        assert!(woke());
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        assert!(!woke());
        // Pruned with its row: the same identity back is news.
        host.apply_engine(&engine_live(vec![]));
        assert!(host.engine_woken.is_empty());
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        assert!(woke());
        // Another process's Claude row is a successor: not an end, and `observe` re-points it.
        let mut host = engine_host(dir.path());
        host.rediscover = Some(wake.clone());
        host.apply_engine(&engine_live(vec![engine_row("idle")]));
        assert!(!woke(), "mirrored at this pid and birth");
        let mut moved = engine_row("live");
        moved.pid += 1;
        host.apply_engine(&engine_live(vec![moved]));
        assert!(host.mirrors[SID].ended_at.is_none() && host.ended_pending.is_empty());
        assert_eq!(host.mirrors[SID].native.status, "busy");
        assert!(woke());
    }

    #[test]
    fn local_sessions_every_provider_gets_its_network_id_and_only_claude_its_status() {
        let dir = tempfile::tempdir().unwrap();
        let mut codex = test_mirror(SID, "g");
        codex.native.status = "busy".into();
        codex.native.process.pid = 2_147_483_000;
        let mut gone = test_mirror(SID_B, "g");
        gone.ended_at = Some(1);
        let mut host = test_host(dir.path(), vec![(SID, codex), (SID_B, gone), ("other", test_mirror("other", "g"))]);
        let mut row = engine_row("idle");
        row.provider = "codex".into();
        row.key = "abc/codex:live".into();
        let mut ended = engine_ended(Some("moved-on"));
        ended.provider = "codex".into();
        ended.conversation = SID_B.into();
        ended.key = "abc/codex:ended".into();
        // The same conversation under Claude is another session.
        let foreign = EngineEnded { conversation: "other".into(), ..engine_ended(None) };
        host.apply_engine(&Ok(EngineView { live: vec![row], ended: vec![ended, foreign] }));
        assert_eq!(host.mirrors[SID].network_id.as_deref(), Some("abc/codex:live"));
        assert_eq!(host.mirrors[SID].native.status, "busy");
        assert!(host.mirrors[SID].ended_at.is_none());
        assert_eq!(host.mirrors[SID_B].network_id.as_deref(), Some("abc/codex:ended"));
        assert!(host.mirrors["other"].network_id.is_none());
        assert!(host.heartbeat_dirty.contains(SID) && !host.heartbeat_dirty.contains("other"));
        let stored: Mirror = private_read(&dir.path().join(format!("{SID}.json"))).unwrap();
        assert_eq!(stored.network_id.as_deref(), Some("abc/codex:live"));
    }

    #[test]
    fn local_sessions_register_and_heartbeat_carry_the_network_id() {
        let mut mirror = test_mirror(SID, "g");
        mirror.session_id = Some(SID.into());
        mirror.network_id = Some("abc/codex:u".into());
        let mirrors = BTreeMap::from([(SID.to_owned(), mirror.clone())]);
        let registered = BTreeSet::from([SID.to_owned()]);
        // The flag is process-wide and other tests' sockets reset it, hence the retry.
        let carried = |read: &dyn Fn() -> Value| {
            (0..1000).any(|_| {
                super::super::session_states::set_reads_network_id(true);
                read()["networkSessionId"] == "abc/codex:u"
            })
        };
        assert!(carried(&|| registration(&mirror)["session"].clone()));
        assert!(carried(&|| device_heartbeats(&mirrors, &registered, false).unwrap()[0]["sessions"][0].clone()));
    }

    #[test]
    fn local_sessions_a_row_gone_for_one_poll_is_no_end_and_two_are() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        {
            let m = host.mirrors.get_mut(SID).unwrap();
            m.session_id = Some(SID.into());
            m.registration.acknowledged = Some("g".into());
        }
        let ended = Ok(EngineView { live: vec![], ended: vec![engine_ended(Some("session-end"))] });
        for gap in [engine_live(vec![]), ended] {
            host.apply_engine(&gap);
            assert!(host.mirrors[SID].ended_at.is_none());
            assert!(host.ended_frame(&mut BTreeSet::new()).unwrap().is_none(), "no end frame");
            host.apply_engine(&engine_live(vec![engine_row("live")]));
        }
        // A fault between two misses is not a live row, and not a miss.
        host.apply_engine(&engine_live(vec![]));
        host.apply_engine(&Err("down".into()));
        assert!(host.mirrors[SID].ended_at.is_none());
        host.apply_engine(&engine_live(vec![]));
        assert_eq!(host.mirrors[SID].ended_reason.as_deref(), Some("process-exited"));
        assert!(host.ended_frame(&mut BTreeSet::new()).unwrap().is_some());
        assert!(host.engine_missed.is_empty());
    }

    #[test]
    fn local_sessions_a_codex_fork_still_ends_the_origin_it_moved_out_of() {
        let dir = tempfile::tempdir().unwrap();
        let origin = test_mirror(SID, "gen-1");
        let key = native_key(&origin.native);
        let mut host = test_host(dir.path(), vec![(key.as_str(), origin.clone())]);
        let mut fork = test_native(SID_B);
        fork.forked_from = Some(SID.into());
        host.observe(vec![origin.native.clone(), fork.clone()]).unwrap();
        assert!(host.mirrors[&key].ended_at.is_none(), "still listed beside its fork");
        host.observe(vec![fork]).unwrap();
        assert_eq!(host.mirrors[&key].ended_reason.as_deref(), Some("exited"));
    }

    fn ended_claude(dir: &Path, job: Option<&str>, reason: &str) -> (Host, String, NativeSession) {
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.provider = "claude".into();
        mirror.native.job = job.map(Into::into);
        mirror.native.status = "ended".into();
        mirror.ended_at = Some(epoch_secs() + 1);
        mirror.ended_reason = Some(reason.into());
        let key = native_key(&mirror.native);
        let mut native = mirror.native.clone();
        native.status = "idle".into();
        (test_host(dir, vec![(key.as_str(), mirror)]), key, native)
    }

    #[test]
    fn local_sessions_a_row_the_engine_lists_revives_a_false_end_on_the_same_process() {
        for job in [Some("1234abcd"), None] {
            for reason in ["process-exited", "exited"] {
                let dir = tempfile::tempdir().unwrap();
                let (mut host, key, native) = ended_claude(dir.path(), job, reason);
                host.observe(vec![]).unwrap();
                assert!(host.mirrors[&key].ended_at.is_some(), "no row in this pass: nothing says it runs");
                host.observe(vec![native]).unwrap();
                assert!(host.mirrors[&key].ended_at.is_none(), "{job:?} {reason}");
            }
        }
    }

    #[test]
    fn local_sessions_a_discovery_read_from_before_the_end_does_not_revive_it() {
        let dir = tempfile::tempdir().unwrap();
        let (mut host, key, native) = ended_claude(dir.path(), None, "process-exited");
        host.mirrors.get_mut(&key).unwrap().ended_at_ms = Some(5000);
        host.discovery_read_ms = Some(4999);
        host.observe(vec![native.clone()]).unwrap();
        assert!(host.mirrors[&key].ended_at.is_some(), "read before the end: it says nothing about the end");
        host.discovery_read_ms = Some(5001);
        host.observe(vec![native]).unwrap();
        assert!(host.mirrors[&key].ended_at.is_none(), "read after the end: the session runs");
    }

    #[tokio::test]
    async fn local_sessions_the_engine_poller_stops_with_its_host() {
        let (tx, mut rx) = tokio::sync::watch::channel(None);
        let poller = tokio::spawn(poll_engine(tx, || async { engine_live(vec![]) }));
        rx.changed().await.unwrap();
        drop(rx);
        tokio::time::timeout(Duration::from_secs(5), poller).await.expect("the poller outlived its Host").unwrap();
    }

    #[test]
    fn local_sessions_a_new_mirror_takes_its_network_id_from_the_latest_engine_answer() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![]);
        let ended = EngineEnded { key: format!("abc/claude:{SID_B}"), conversation: SID_B.into(), ..engine_ended(None) };
        let answer = Ok(EngineView { live: vec![engine_row("idle")], ended: vec![ended] });
        let (_tx, rx) = tokio::sync::watch::channel(Some(Arc::new((Instant::now(), answer))));
        host.engine = Some(rx);
        let claude = |conversation: &str| NativeSession { provider: "claude".into(), ..test_native(conversation) };
        let other = "5eed00f1-5e55-4000-8000-0000000000f1";
        let keys = [SID, SID_B, other].map(|c| native_key(&claude(c)));
        host.observe(vec![claude(SID), claude(SID_B), claude(other)]).unwrap();
        assert_eq!(host.mirrors[&keys[0]].network_id, Some(format!("abc/claude:{SID}")));
        assert_eq!(host.mirrors[&keys[1]].network_id, Some(format!("abc/claude:{SID_B}")));
        assert_eq!(host.mirrors[&keys[2]].network_id, None);
    }

    #[test]
    fn local_sessions_a_deferred_end_says_unknown_for_claude_and_idle_for_the_others() {
        for (provider, word) in [("claude", "unknown"), ("codex", "idle")] {
            let dir = tempfile::tempdir().unwrap();
            let mut mirror = test_mirror(SID, "g");
            mirror.native.provider = provider.into();
            mirror.native.status = "busy".into();
            mirror.registration.pending = true;
            let mut host = test_host(dir.path(), vec![(SID, mirror)]);
            host.end_session(SID, "process-exited").unwrap();
            assert_eq!(host.mirrors[SID].deferred_end_reason.as_deref(), Some("process-exited"));
            assert_eq!(host.mirrors[SID].native.status, word, "{provider}");
        }
    }

    #[test]
    fn local_sessions_a_meant_end_stays_ended_though_the_engine_still_lists_it() {
        for reason in [lifecycle::ON_PURPOSE, lifecycle::IDLE_STOPPED, lifecycle::LOGIN_SESSION_ENDED] {
            let dir = tempfile::tempdir().unwrap();
            let (mut host, key, native) = ended_claude(dir.path(), None, reason);
            host.observe(vec![native]).unwrap();
            assert!(host.mirrors[&key].ended_at.is_some(), "{reason}");
        }
        // End asked of its session, or of its holder.
        const SESSION: &str = "5eed00a1-5e55-4000-8000-0000000000a1";
        const HOLDER: &str = "5eed00a2";
        for asked in [SESSION, HOLDER] {
            let dir = tempfile::tempdir().unwrap();
            let (mut host, key, native) = ended_claude(dir.path(), None, "process-exited");
            {
                let m = host.mirrors.get_mut(&key).unwrap();
                m.session_id = Some(SESSION.into());
                m.native.holder = Some(HOLDER.into());
            }
            crate::holder::manager::end_asked_now(asked);
            host.observe(vec![native]).unwrap();
            assert!(host.mirrors[&key].ended_at.is_some(), "{asked}");
            crate::holder::manager::end_asked_forget(asked);
        }
    }

    #[test]
    fn local_sessions_a_live_row_disarms_a_false_deferred_end_but_not_a_meant_one() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        host.mirrors.get_mut(SID).unwrap().registration.pending = true;
        host.apply_engine(&engine_live(vec![]));
        host.apply_engine(&engine_live(vec![]));
        assert_eq!(host.mirrors[SID].deferred_end_reason.as_deref(), Some("process-exited"));
        host.apply_engine(&engine_live(vec![engine_row("live")]));
        assert_eq!(host.mirrors[SID].deferred_end_reason, None);
        assert_eq!(host.mirrors[SID].native.status, "busy");
        let stored: Mirror = private_read(&dir.path().join(format!("{SID}.json"))).unwrap();
        assert_eq!(stored.deferred_end_reason, None, "saved");
        host.mirrors.get_mut(SID).unwrap().registration.pending = false;
        host.finish_deferred_end(SID).unwrap();
        assert!(host.mirrors[SID].ended_at.is_none(), "nothing left armed");
        // The app ending a stranded or idle holder: its row outlives the ask by a moment.
        for reason in [lifecycle::IDLE_STOPPED, lifecycle::LOGIN_SESSION_ENDED] {
            host.mirrors.get_mut(SID).unwrap().deferred_end_reason = Some(reason.into());
            host.apply_engine(&engine_live(vec![engine_row("live")]));
            assert_eq!(host.mirrors[SID].deferred_end_reason.as_deref(), Some(reason));
        }
    }

    #[test]
    fn local_sessions_engine_tick_drops_an_answer_read_before_the_last_observe() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        let (tx, rx) = tokio::sync::watch::channel(None);
        host.engine = Some(rx);
        let read_at = Instant::now();
        let native = host.mirrors[SID].native.clone();
        host.observe(vec![native]).unwrap();
        tx.send(Some(Arc::new((read_at, engine_live(vec![]))))).unwrap();
        host.engine_tick();
        assert!(host.engine_missed.is_empty(), "a dropped answer is no miss");
        tx.send(Some(Arc::new((read_at, engine_live(vec![engine_row("live")]))))).unwrap();
        host.engine_tick();
        assert_ne!(host.mirrors[SID].native.status, "busy");
        tx.send(Some(Arc::new((Instant::now(), engine_live(vec![engine_row("live")]))))).unwrap();
        host.engine_tick();
        assert_eq!(host.mirrors[SID].native.status, "busy");
    }

    #[test]
    fn local_sessions_an_end_forgets_the_successor_it_ended_with() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        let mut next = host.mirrors[SID].native.clone();
        next.process.birth_micros += 1;
        host.mirrors.get_mut(SID).unwrap().registration.successor = Some(next);
        host.apply_engine(&engine_live(vec![]));
        host.apply_engine(&engine_live(vec![]));
        assert!(host.mirrors[SID].ended_at.is_some());
        let m = host.mirrors.get_mut(SID).unwrap();
        assert!(m.registration.successor.is_none());
        assert!(!m.advance(), "an ended mirror does not take on another process");
        assert_eq!(m.generation, "g");
        host.observe(vec![]).unwrap();
        assert!(host.mirrors[SID].ended_at.is_some(), "and is not revived by it");
    }

    #[test]
    fn local_sessions_an_engine_fault_keeps_a_persons_own_exit() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        host.mirrors.get_mut(SID).unwrap().native.process.pid = 2_147_483_000;
        host.left_on_purpose.insert(SID.into());
        host.apply_engine(&Err("down".into()));
        assert_eq!(host.mirrors[SID].ended_reason.as_deref(), Some(lifecycle::ON_PURPOSE));
    }

    #[test]
    fn local_sessions_a_blocked_job_at_its_shell_reads_idle_not_waiting() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = engine_host(dir.path());
        host.mirrors.get_mut(SID).unwrap().native.attention.note = Some("press Resume".into());
        let blocked = |word: &str| EngineRow {
            kind: Some("background".into()),
            job: Some(("deadbeef".into(), "blocked".into())),
            waiting_for: Some(word.into()),
            ..engine_row("waiting")
        };
        host.apply_engine(&engine_live(vec![blocked("input needed")]));
        let native = &host.mirrors[SID].native;
        assert_eq!((native.status.as_str(), native.attention.waiting_for.as_deref(), native.attention.note.as_deref()), ("idle", None, Some("press Resume")));
        host.apply_engine(&engine_live(vec![blocked("permission prompt")]));
        let native = &host.mirrors[SID].native;
        assert_eq!((native.status.as_str(), native.attention.waiting_for.as_deref()), ("waiting-approval", Some("permission prompt")));
        host.mirrors.get_mut(SID).unwrap().waiting = Some(held_prompt(""));
        host.apply_engine(&engine_live(vec![blocked("input needed")]));
        assert_eq!(host.mirrors[SID].native.status, "waiting-approval", "a held prompt still is one");
    }

    fn hook(provider: lifecycle::Provider, event: &str, session: &str) -> lifecycle::HookEvent {
        lifecycle::HookEvent {
            provider,
            source_process: Some(lifecycle::HookProcess::of(&process(std::process::id()).unwrap())),
            event: event.into(),
            session_id: session.into(),
            transcript_path: None,
            cwd: None,
            notification_type: None,
            message: None,
            reason: None,
            last_assistant_message: None,
            tool_name: None,
            tool_use_id: None,
            waiting_text: None,
            held: None,
        }
    }

    #[tokio::test]
    async fn local_sessions_device_heartbeat_lists_live_sessions_and_rejected_reregisters() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(
            dir.path(),
            vec![
                ("a-session", test_mirror(SID, "gen-1")),
                ("b-session", test_mirror(SID_B, "gen-2")),
            ],
        );
        host.heartbeat_interval = Duration::from_millis(50);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            for (generation, sid) in [("gen-1", SID), ("gen-2", SID_B)] {
                let frame = next_frame(&mut socket).await;
                assert_eq!(frame["type"], "local.session.register");
                assert_eq!(frame["session"]["generation"], generation, "keys register in order");
                reply(&mut socket, json!({"type":"local.session.registered","sessionId":sid,
                    "generation":generation,"afterSeq":0,"serverTime":1000,"reconcile":null,
                    "features":["liveTerminal","remoteInput"]})).await;
            }
            // A nudge for a session this Mac does not hold is ignored: no reply, no error.
            reply(&mut socket, json!({"type":"local.session.watch",
                "sessionId":"ccccdddd-eeee-4fff-8aaa-bbbb00001111","generation":"gen-1"})).await;
            // So is input for a session nobody watches, or that is malformed: the connection
            // stays up and the next frame is still the heartbeat.
            reply(&mut socket, json!({"type":"local.session.input","sessionId":SID,
                "generation":"gen-1","epoch":3,"data":"bHMgLWxhDQ=="})).await;
            reply(&mut socket, json!({"type":"local.session.input","sessionId":"../x"})).await;
            // One frame for the device, not one per session.
            let hb = next_frame(&mut socket).await;
            assert_eq!(hb["liveTerminal"], true, "advertised to a relay that lists liveTerminal");
            assert_eq!(hb["remoteInput"], true, "advertised to a relay that lists remoteInput");
            let golden: Value = serde_json::from_str(GOLDEN_HEARTBEAT).unwrap();
            assert_eq!(hb["type"], "local.device.heartbeat");
            assert_eq!(frame_keys(&hb), frame_keys(&golden));
            assert_eq!(hb["sessions"].as_array().unwrap().len(), 2);
            assert_eq!(
                frame_keys(&hb["sessions"][0]),
                frame_keys(&golden["sessions"][0]),
                "every per-session field the contract names, and no other"
            );
            assert_eq!(hb["sessions"][0]["sessionId"], SID);
            assert_eq!(hb["sessions"][1]["sessionId"], SID_B);
            assert_eq!(
                hb["sessions"][0]["status"], "unknown",
                "the relay rejects the whole frame over one unknown status word"
            );
            let ack = json!({"type":"local.device.heartbeat.ack","rejected":[SID_B],
                "reconcile":[],"serverTime":1010,"features":[],"watching":[]});
            let golden_ack: Value = serde_json::from_str(GOLDEN_HEARTBEAT_ACK).unwrap();
            assert_eq!(frame_keys(&ack), frame_keys(&golden_ack));
            reply(&mut socket, ack).await;
            let again = next_frame(&mut socket).await;
            assert_eq!(again["type"], "local.session.register");
            assert_eq!(
                again["session"]["generation"], "gen-2",
                "a rejected session re-registers; the accepted one does not"
            );
            socket.close(None).await.unwrap();
        });
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(5), host.connection(&mut socket, &mut rx, "alice"))
            .await;
        server.await.unwrap();
    }

    #[tokio::test]
    async fn local_sessions_ended_frame_is_sent_once_and_stops_heartbeats() {
        let dir = tempfile::tempdir().unwrap();
        let persisted = dir.path().join("a-session.json");
        let acked = persisted.clone();
        let mut host = test_host(dir.path(), vec![("a-session", test_mirror(SID, "gen-1"))]);
        host.heartbeat_interval = Duration::from_millis(50);
        let (hooks_tx, hooks_rx) = tokio::sync::mpsc::channel(1);
        host.hooks = Some(hooks_rx);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let frame = next_frame(&mut socket).await;
            assert_eq!(frame["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,
                "generation":"gen-1","afterSeq":0,"serverTime":1000,"reconcile":null})).await;
            // End it only once that ACK is durable: a hook landing first would end a
            // session the relay has never heard of, which has no end to report.
            let mut tick = tokio::time::interval(Duration::from_millis(20));
            tokio::time::timeout(Duration::from_secs(2), async {
                loop {
                    tick.tick().await;
                    if private_read::<Mirror>(&acked).is_ok_and(|m| m.session_id.is_some()) {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            hooks_tx.send(hook(lifecycle::Provider::Codex, "SessionEnd", SID)).await.unwrap();
            // Heartbeats keep coming until the hook lands, and while it is live the
            // session is in every one of them.
            let ended = loop {
                let frame = next_frame(&mut socket).await;
                if frame["type"] != "local.device.heartbeat" {
                    break frame;
                }
                assert_eq!(frame["sessions"][0]["sessionId"], SID);
                reply(&mut socket, json!({"type":"local.device.heartbeat.ack","rejected":[],
                    "reconcile":[],"serverTime":1005})).await;
            };
            let golden: Value = serde_json::from_str(GOLDEN_ENDED).unwrap();
            assert_eq!(ended["type"], "local.session.ended");
            assert_eq!(frame_keys(&ended), frame_keys(&golden));
            assert_eq!(ended["sessionId"], SID);
            assert_eq!(ended["generation"], "gen-1");
            assert_eq!(ended["reason"], "exited");
            assert!(ended["endedAt"].as_u64().is_some_and(|at| at > 1_700_000_000));
            let ack = json!({"type":"local.session.ended.ack","sessionId":SID});
            let golden_ack: Value = serde_json::from_str(GOLDEN_ENDED_ACK).unwrap();
            assert_eq!(frame_keys(&ack), frame_keys(&golden_ack));
            reply(&mut socket, ack).await;
            // The cadence here is 50ms, so anything at all would be a frame naming a
            // session the relay has already been told is over.
            let quiet = tokio::time::timeout(Duration::from_millis(400), socket.next()).await;
            assert!(
                quiet.is_err(),
                "an ended session is never heartbeated, re-registered or re-ended: {quiet:?}"
            );
            socket.close(None).await.unwrap();
        });
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(5), host.connection(&mut socket, &mut rx, "alice"))
            .await;
        server.await.unwrap();
        let stored: Mirror = private_read(&persisted).unwrap();
        assert!(stored.ended_sent, "an acknowledged end is durable across restarts");
    }

    /// Started from the web or a phone, ended offline before it wrote anything: the relay has
    /// never heard of it. It registers once to exist, then its end names the start.
    #[tokio::test]
    async fn local_sessions_a_bound_run_that_ended_unregistered_and_empty_is_still_reported() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.native.status = "ended".into();
        mirror.ended_at = Some(1_700_000_042);
        private_write(
            &dir.path().join(format!("start-{}.json",
                fingerprint(&json!([mirror.native.provider, mirror.native.conversation]).to_string()))),
            &json!({"startId":"11111111-1111-4111-8111-111111111111","provider":mirror.native.provider,
                "conversation":mirror.native.conversation,"launcher":process(std::process::id()).unwrap(),
                "boundGeneration":"gen-1"}),
        )
        .unwrap();
        assert!(!needs_ended_recovery(&mirror), "nothing to drain");
        // Its neighbour has no bound start: retired silently, as before.
        let mut silent = test_mirror(SID_B, "gen-2");
        silent.native.status = "ended".into();
        silent.ended_at = Some(1_700_000_043);
        let mut host = test_host(dir.path(), vec![("a-session", mirror), ("b-session", silent)]);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            assert_eq!(registration["session"]["nativeConversationId"], SID);
            assert_eq!(registration["session"]["controlUnavailableReason"], "runtime-unverified");
            assert!(registration["session"].get("startId").is_none());
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,
                "generation":registration["session"]["generation"],"afterSeq":0,"serverTime":1000,
                "features":["endedStartId"],"reconcile":null})).await;
            let ended = next_frame(&mut socket).await;
            assert_eq!(ended["type"], "local.session.ended", "one register, then the end");
            assert_eq!(ended["sessionId"], SID);
            assert_eq!(ended["startId"], "11111111-1111-4111-8111-111111111111");
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            let quiet = tokio::time::timeout(Duration::from_millis(400), socket.next()).await;
            assert!(quiet.is_err(), "nothing registers again and the unbound end stays silent: {quiet:?}");
            socket.close(None).await.unwrap();
        });
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            host.connection(&mut socket, &mut rx, "alice"),
        )
        .await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("a-session.json")).unwrap();
        assert!(stored.ended_sent && stored.session_id.as_deref() == Some(SID));
    }

    /// A drained end retried on a new socket: nothing registers, so the Mac asks with a
    /// heartbeat at once and the end follows that ack, in the shape this relay reads.
    #[tokio::test]
    async fn local_sessions_a_bound_end_on_a_fresh_socket_follows_the_first_heartbeat_ack() {
        for (features, expected) in [
            (Some(json!(["endedStartId"])), json!("11111111-1111-4111-8111-111111111111")),
            (Some(json!([])), Value::Null),
            (None, Value::Null),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let mut mirror = test_mirror(SID, "gen-1");
            mirror.session_id = Some(SID.into());
            mirror.registration.acknowledged = Some("gen-1".into());
            mirror.native.status = "ended".into();
            mirror.ended_at = Some(1_700_000_042);
            private_write(
                &dir.path().join(format!("start-{}.json",
                    fingerprint(&json!([mirror.native.provider, mirror.native.conversation]).to_string()))),
                &json!({"startId":"11111111-1111-4111-8111-111111111111","provider":mirror.native.provider,
                    "conversation":mirror.native.conversation,"launcher":process(std::process::id()).unwrap(),
                    "boundGeneration":"gen-1"}),
            )
            .unwrap();
            let mut host = test_host(dir.path(), vec![("a-session", mirror)]);
            // What an earlier socket said is not what this relay reads.
            host.relay_reads_ended_start = Some(true);
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let server = tokio::spawn(async move {
                let (tcp, _) = listener.accept().await.unwrap();
                let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
                let asked = next_frame(&mut socket).await;
                assert_eq!(asked["type"], "local.device.heartbeat", "the end waits for an ack");
                let mut ack = json!({"type":"local.device.heartbeat.ack","rejected":[],"serverTime":1000,"reconcile":[]});
                if let Some(features) = features {
                    ack["features"] = features;
                }
                reply(&mut socket, ack).await;
                let ended = next_frame(&mut socket).await;
                assert_eq!(ended["type"], "local.session.ended");
                assert_eq!(ended["startId"], expected);
                reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
                socket.close(None).await.unwrap();
            });
            let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
            let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
                .await
                .unwrap();
            let _ = tokio::time::timeout(
                Duration::from_secs(5),
                host.connection(&mut socket, &mut rx, "alice"),
            )
            .await;
            server.await.unwrap();
        }
    }

    #[tokio::test]
    async fn local_sessions_an_end_missed_while_the_relay_was_down_is_reported_on_reconnect() {
        let dir = tempfile::tempdir().unwrap();
        let mut reported = test_mirror(SID, "gen-1");
        reported.session_id = Some(SID.into());
        reported.registration.acknowledged = Some("gen-1".into());
        reported.native.status = "ended".into();
        reported.ended_at = Some(1_700_000_042);
        reported.ended_reason = Some("process-exited".into());
        // Ended before the relay ever learned it existed: there is nothing to tell.
        let mut silent = test_mirror(SID_B, "gen-2");
        silent.native.status = "ended".into();
        silent.ended_at = Some(1_700_000_043);
        silent.ended_reason = Some("exited".into());
        let mut host = test_host(
            dir.path(),
            vec![("a-session", reported), ("b-session", silent)],
        );
        host.heartbeat_interval = Duration::from_millis(50);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let ended = next_frame(&mut socket).await;
            assert_eq!(ended["type"], "local.session.ended", "first frame after connect");
            assert_eq!(ended["sessionId"], SID);
            assert_eq!(ended["generation"], "gen-1");
            assert_eq!(ended["reason"], "process-exited");
            assert_eq!(ended["endedAt"], 1_700_000_042u64);
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            let quiet = tokio::time::timeout(Duration::from_millis(400), socket.next()).await;
            assert!(
                quiet.is_err(),
                "an end the relay could never have known is retired silently: {quiet:?}"
            );
            socket.close(None).await.unwrap();
        });
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(5), host.connection(&mut socket, &mut rx, "alice"))
            .await;
        server.await.unwrap();
        let reported: Mirror = private_read(&dir.path().join("a-session.json")).unwrap();
        let silent: Mirror = private_read(&dir.path().join("b-session.json")).unwrap();
        assert!(
            reported.ended_sent && silent.ended_sent,
            "both ends are retired, one on the wire and one silently"
        );
    }

    #[tokio::test]
    async fn local_sessions_a_resumed_session_reports_its_second_end_too() {
        let dir = tempfile::tempdir().unwrap();
        // Keyed by its real identity: only then does a listing for the same
        // provider/profile/conversation land on this mirror and resume it.
        let key = native_key(&test_native(SID));
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.session_id = Some(SID.into());
        mirror.registration.acknowledged = Some("gen-1".into());
        mirror.native.status = "ended".into();
        mirror.ended_at = Some(1_700_000_042);
        mirror.ended_reason = Some("exited".into());
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        host.heartbeat_interval = Duration::from_secs(60);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["type"], "local.session.ended");
            assert_eq!(first["reason"], "exited");
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            // A new process on the same identity: the session is back, and its own end
            // is news the relay has not heard.
            let mut resumed = test_native(SID);
            resumed.process.pid = 2_147_483_000; // and this one is already gone
            tx.send((vec![resumed], vec![], vec![], epoch_ms())).await.unwrap();
            // It may register before the liveness tick finds that pid dead; either
            // order ends with the second end on the wire.
            let second = loop {
                let frame = next_frame(&mut socket).await;
                if frame["type"] != "local.session.register" {
                    break frame;
                }
                reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,
                    "generation":frame["session"]["generation"],"afterSeq":0,"serverTime":1000,
                    "reconcile":null})).await;
            };
            assert_eq!(second["type"], "local.session.ended");
            assert_eq!(second["reason"], "process-exited");
            assert_ne!(
                second["generation"], first["generation"],
                "the second end is the resumed session's, not a replay of the first"
            );
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            socket.close(None).await.unwrap();
        });
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(5), host.connection(&mut socket, &mut rx, "alice"))
            .await;
        server.await.unwrap();
    }

    #[tokio::test]
    async fn local_sessions_an_ended_session_drains_its_last_bytes_before_its_end_is_reported() {
        let dir = tempfile::tempdir().unwrap();
        let persisted = dir.path().join("a-session.json");
        let acked = persisted.clone();
        let mut mirror = test_mirror("s", "gen-1");
        mirror.native = observed_fixture(dir.path());
        // A running Claude conversation is in Claude's own per-pid registry.
        let sessions = dir.path().join(".claude/sessions");
        fs::create_dir_all(&sessions).unwrap();
        fs::write(sessions.join(format!("{}.json", mirror.native.process.pid)),
            format!("{{\"sessionId\":\"{}\"}}", mirror.native.conversation)).unwrap();
        // Nothing to read yet: the bytes arrive only once the session is registered,
        // so no upload pass can have carried them away before it ends.
        let transcript = mirror.native.transcript.clone().unwrap();
        fs::write(&transcript, "").unwrap();
        let mut host = test_host(dir.path(), vec![("a-session", mirror)]);
        host.heartbeat_interval = Duration::from_secs(60);
        let (engine_tx, engine_rx) = tokio::sync::watch::channel(None);
        host.engine = Some(engine_rx);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let frame = next_frame(&mut socket).await;
            assert_eq!(frame["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,
                "generation":frame["session"]["generation"],"afterSeq":0,"serverTime":1000,
                "reconcile":null})).await;
            let mut tick = tokio::time::interval(Duration::from_millis(20));
            tokio::time::timeout(Duration::from_secs(2), async {
                loop {
                    tick.tick().await;
                    if private_read::<Mirror>(&acked).is_ok_and(|m| m.session_id.is_some()) {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            // The conversation's last words, written and then immediately ended: the
            // only reader that can still see them is the drain gate in `ended_frame`.
            let mut file = fs::OpenOptions::new().append(true).open(&transcript).unwrap();
            writeln!(file,"{}",json!({"type":"assistant","sessionId":"s","version":"2.1.268","message":{"role":"assistant","content":[{"type":"text","text":"short-lived public output"}]}})).unwrap();
            drop(file);
            // Two polls with no row: the end.
            engine_tx.send(Some(Arc::new((Instant::now(), engine_live(vec![]))))).unwrap();
            tokio::time::sleep(Duration::from_millis(300)).await;
            engine_tx.send(Some(Arc::new((Instant::now(), engine_live(vec![]))))).unwrap();
            let mut drained = false;
            let ended = loop {
                let frame = next_frame(&mut socket).await;
                if frame["type"] != "local.session.events" {
                    break frame;
                }
                assert_eq!(frame["events"][0]["text"], "short-lived public output");
                drained = true;
                reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                    "batchId":frame["batchId"],"afterSeq":1})).await;
            };
            assert!(
                drained,
                "the end of the conversation must travel before the end is reported"
            );
            assert_eq!(ended["type"], "local.session.ended");
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            socket.close(None).await.unwrap();
        });
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(5), host.connection(&mut socket, &mut rx, "alice"))
            .await;
        server.await.unwrap();
        let stored: Mirror = private_read(&persisted).unwrap();
        assert_eq!(stored.after_seq, 1, "the last bytes were acknowledged");
        assert!(stored.pending.is_none() && stored.queue.is_empty() && stored.ended_sent);
    }

    #[tokio::test]
    async fn local_sessions_reconnect_registers_ended_mirror_to_drain_unread_output() {
        let dir = tempfile::tempdir().unwrap();
        let native = observed_fixture(dir.path());
        let key = native_key(&native);
        let transcript = native.transcript.clone().unwrap();
        let mut rows = (0..33)
            .map(|_| format!("{}\n", json!({"type":"progress"})))
            .collect::<String>();
        rows.push_str(&fs::read_to_string(&transcript).unwrap());
        fs::write(&transcript, rows).unwrap();
        let mut mirror = test_mirror("s", "gen-1");
        mirror.native = native;
        mirror.native.status = "ended".into();
        mirror.registration.acknowledged = Some("gen-1".into());
        mirror.ended_at = Some(epoch_secs());
        mirror.ended_reason = Some("exited".into());
        // Started from the web or a phone: the relay refuses a start identity on an
        // unverified register, and a refused drain is an end that is never reported.
        private_write(
            &dir.path().join(format!("start-{}.json",
                fingerprint(&json!([mirror.native.provider, mirror.native.conversation]).to_string()))),
            &json!({"startId":"11111111-1111-4111-8111-111111111111","provider":mirror.native.provider,
                "conversation":mirror.native.conversation,"launcher":process(std::process::id()).unwrap(),
                "boundGeneration":"gen-1"}),
        )
        .unwrap();
        assert!(start_links::linked(dir.path(), &mirror.native, &mirror.generation).is_some());
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        host.heartbeat_interval = Duration::from_secs(60);

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            assert!(registration["session"].get("startId").is_none());
            assert_eq!(registration["session"]["status"], "idle");
            assert_eq!(registration["session"]["activeTurnId"], Value::Null);
            assert_eq!(registration["session"]["controlUnavailableReason"], "runtime-unverified");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,
                "generation":registration["session"]["generation"],"afterSeq":0,"serverTime":1000,
                "features":["endedStartId"],"reconcile":null})).await;

            let events = next_frame(&mut socket).await;
            assert_eq!(events["type"], "local.session.events");
            assert_eq!(events["events"][0]["text"], "short-lived public output");
            reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                "batchId":events["batchId"],"afterSeq":1})).await;

            let ended = next_frame(&mut socket).await;
            assert_eq!(ended["type"], "local.session.ended");
            // The whole run may have been offline: the end names the start it was bound to.
            assert_eq!(ended["startId"], "11111111-1111-4111-8111-111111111111");
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            socket.close(None).await.unwrap();
        });
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            host.connection(&mut socket, &mut rx, "alice"),
        )
        .await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join(format!("{key}.json"))).unwrap();
        assert_eq!(stored.after_seq, 1);
        assert!(stored.pending.is_none() && stored.queue.is_empty() && stored.ended_sent);
        assert!(fs::metadata(transcript).is_ok());
    }

    #[tokio::test]
    async fn local_sessions_an_older_relay_gets_no_start_identity_on_an_end() {
        let dir = tempfile::tempdir().unwrap();
        let native = observed_fixture(dir.path());
        let key = native_key(&native);
        let transcript = native.transcript.clone().unwrap();
        let mut rows = (0..33)
            .map(|_| format!("{}\n", json!({"type":"progress"})))
            .collect::<String>();
        rows.push_str(&fs::read_to_string(&transcript).unwrap());
        fs::write(&transcript, rows).unwrap();
        let mut mirror = test_mirror("s", "gen-1");
        mirror.native = native;
        mirror.native.status = "ended".into();
        mirror.registration.acknowledged = Some("gen-1".into());
        mirror.ended_at = Some(epoch_secs());
        mirror.ended_reason = Some("exited".into());
        // Started from the web or a phone: the relay refuses a start identity on an
        // unverified register, and a refused drain is an end that is never reported.
        private_write(
            &dir.path().join(format!("start-{}.json",
                fingerprint(&json!([mirror.native.provider, mirror.native.conversation]).to_string()))),
            &json!({"startId":"11111111-1111-4111-8111-111111111111","provider":mirror.native.provider,
                "conversation":mirror.native.conversation,"launcher":process(std::process::id()).unwrap(),
                "boundGeneration":"gen-1"}),
        )
        .unwrap();
        assert!(start_links::linked(dir.path(), &mirror.native, &mirror.generation).is_some());
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        host.heartbeat_interval = Duration::from_secs(60);

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let registration = next_frame(&mut socket).await;
            assert_eq!(registration["type"], "local.session.register");
            assert!(registration["session"].get("startId").is_none());
            assert_eq!(registration["session"]["status"], "idle");
            assert_eq!(registration["session"]["activeTurnId"], Value::Null);
            assert_eq!(registration["session"]["controlUnavailableReason"], "runtime-unverified");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,
                "generation":registration["session"]["generation"],"afterSeq":0,"serverTime":1000,
                "features":[],"reconcile":null})).await;

            let events = next_frame(&mut socket).await;
            assert_eq!(events["type"], "local.session.events");
            assert_eq!(events["events"][0]["text"], "short-lived public output");
            reply(&mut socket, json!({"type":"local.session.events.ack","sessionId":SID,
                "batchId":events["batchId"],"afterSeq":1})).await;

            let ended = next_frame(&mut socket).await;
            assert_eq!(ended["type"], "local.session.ended");
            assert!(ended.get("startId").is_none(), "an older relay refuses the unknown field");
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            socket.close(None).await.unwrap();
        });
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            host.connection(&mut socket, &mut rx, "alice"),
        )
        .await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join(format!("{key}.json"))).unwrap();
        assert_eq!(stored.after_seq, 1);
        assert!(stored.pending.is_none() && stored.queue.is_empty() && stored.ended_sent);
        assert!(fs::metadata(transcript).is_ok());
    }

    #[tokio::test]
    async fn local_sessions_a_dirty_session_heartbeats_early_then_waits_out_the_throttle() {
        let dir = tempfile::tempdir().unwrap();
        // A minute of cadence: only the dirty path can produce a heartbeat here.
        let mut host = test_host(dir.path(), vec![("a-session", test_mirror(SID, "gen-1"))]);
        host.heartbeat_interval = Duration::from_secs(60);
        host.heartbeat_dirty.insert("a-session".into());
        let (hooks_tx, hooks_rx) = tokio::sync::mpsc::channel(1);
        host.hooks = Some(hooks_rx);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let frame = next_frame(&mut socket).await;
            assert_eq!(frame["type"], "local.session.register");
            reply(&mut socket, json!({"type":"local.session.registered","sessionId":SID,
                "generation":"gen-1","afterSeq":0,"serverTime":1000,"reconcile":null})).await;
            // Dirty, so it does not wait out the minute.
            let early = next_frame(&mut socket).await;
            assert_eq!(early["type"], "local.device.heartbeat");
            assert_eq!(early["sessions"][0]["status"], "unknown");
            reply(&mut socket, json!({"type":"local.device.heartbeat.ack","rejected":[],
                "reconcile":[],"serverTime":1001})).await;
            // Dirty again straight away: the throttle holds it for a second, and the
            // minute-long cadence cannot be what releases it.
            let sent = Instant::now();
            hooks_tx
                .send(hook(lifecycle::Provider::Codex, "UserPromptSubmit", SID))
                .await
                .unwrap();
            let quiet = tokio::time::timeout(Duration::from_millis(400), socket.next()).await;
            assert!(quiet.is_err(), "early heartbeats are throttled to one a second: {quiet:?}");
            let second = next_frame(&mut socket).await;
            assert_eq!(second["type"], "local.device.heartbeat");
            assert_eq!(second["sessions"][0]["status"], "busy", "the field that moved");
            assert!(
                sent.elapsed() >= Duration::from_millis(500),
                "released before the throttle was out: {:?}",
                sent.elapsed()
            );
            reply(&mut socket, json!({"type":"local.device.heartbeat.ack","rejected":[],
                "reconcile":[],"serverTime":1002})).await;
            socket.close(None).await.unwrap();
        });
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(5), host.connection(&mut socket, &mut rx, "alice"))
            .await;
        server.await.unwrap();
        assert!(
            host.heartbeat_dirty.is_empty(),
            "sending the heartbeat clears what made it early"
        );
    }

    #[tokio::test]
    async fn local_sessions_an_ended_ack_answers_only_the_end_it_reported() {
        // The ACK for the first end is deliberately withheld until the session has
        // been resumed AND has ended again, so the reply lands on a mirror whose
        // `ended_at` is Some but is a different end. Holding it is what makes this
        // deterministic; the same interleaving arrives at random otherwise.
        let dir = tempfile::tempdir().unwrap();
        let key = native_key(&test_native(SID));
        let persisted = dir.path().join(format!("{key}.json"));
        let mut mirror = test_mirror(SID, "gen-1");
        mirror.session_id = Some(SID.into());
        mirror.registration.acknowledged = Some("gen-1".into());
        mirror.native.status = "ended".into();
        mirror.ended_at = Some(1_700_000_042);
        mirror.ended_reason = Some("exited".into());
        let mut host = test_host(dir.path(), vec![(key.as_str(), mirror)]);
        host.heartbeat_interval = Duration::from_secs(60);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        let server = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let mut socket = tokio_tungstenite::accept_async(tcp).await.unwrap();
            let first = next_frame(&mut socket).await;
            assert_eq!(first["type"], "local.session.ended");
            assert_eq!(first["endedAt"], 1_700_000_042u64);
            // Still unacknowledged: resume the session and let its new process die.
            let mut resumed = test_native(SID);
            resumed.process.pid = 2_147_483_000;
            tx.send((vec![resumed], vec![], vec![], epoch_ms())).await.unwrap();
            let mut tick = tokio::time::interval(Duration::from_millis(20));
            tokio::time::timeout(Duration::from_secs(3), async {
                loop {
                    tick.tick().await;
                    if private_read::<Mirror>(&persisted)
                        .is_ok_and(|m| m.ended_at.is_some() && m.generation != "gen-1")
                    {
                        break;
                    }
                }
            })
            .await
            .expect("the resumed session ends again while the first ACK is held");
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            let second = next_frame(&mut socket).await;
            assert_eq!(
                second["type"], "local.session.ended",
                "the stale ACK must not retire an end it never reported"
            );
            assert_eq!(second["reason"], "process-exited");
            assert_ne!(second["generation"], first["generation"]);
            reply(&mut socket, json!({"type":"local.session.ended.ack","sessionId":SID})).await;
            socket.close(None).await.unwrap();
        });
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = tokio::time::timeout(Duration::from_secs(8), host.connection(&mut socket, &mut rx, "alice"))
            .await;
        server.await.unwrap();
    }

    #[test]
    fn local_sessions_a_session_too_big_for_one_frame_is_omitted_not_sent() {
        let mut mirrors = BTreeMap::new();
        let mut registered = BTreeSet::new();
        for (index, key) in ["a", "b", "c"].into_iter().enumerate() {
            let mut mirror = test_mirror(SID, "gen-1");
            mirror.session_id = Some(SID.into());
            if index == 1 {
                // Padded past the whole 16 KiB envelope on its own.
                mirror.native.title = Some("t".repeat(9000));
                mirror.native.last_assistant_message = Some("m".repeat(9000));
            }
            mirrors.insert(key.to_string(), mirror);
            registered.insert(key.to_string());
        }
        let frames = device_heartbeats(&mirrors, &registered, false).unwrap();
        assert_eq!(frames.len(), 1, "the two that fit share one frame");
        // Every flag on (agentMessageAsks included) is part of the envelope the limit counts.
        let flagged = device_heartbeats_with_controls(&mirrors, &registered, true, true, true, true, true, true, true, true, true).unwrap();
        for frame in &flagged {
            assert_eq!(frame["agentMessageAsks"], true);
            assert!(ascii_wire_len(frame).unwrap() <= 16384);
        }
        assert_eq!(
            frames[0]["sessions"].as_array().unwrap().len(),
            2,
            "the oversized session is omitted, not sent in a frame the relay drops"
        );
        for frame in &frames {
            assert!(ascii_wire_len(frame).unwrap() <= 16384);
        }
    }

    #[test]
    fn local_sessions_device_heartbeats_chunk_at_twenty_four_sessions() {
        let mut mirrors = BTreeMap::new();
        let mut registered = BTreeSet::new();
        for index in 0..25 {
            let key = format!("session-{index:02}");
            let mut mirror = test_mirror(SID, "gen-1");
            mirror.session_id = Some(SID.into());
            mirrors.insert(key.clone(), mirror);
            registered.insert(key);
        }
        let frames = device_heartbeats(&mirrors, &registered, false).unwrap();
        assert_eq!(frames.len(), 2);
        assert_eq!(
            frames[0]["sessions"].as_array().unwrap().len(),
            HEARTBEAT_SESSIONS_MAX
        );
        assert_eq!(frames[1]["sessions"].as_array().unwrap().len(), 1);
    }

}
