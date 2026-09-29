//! Dedicated paired-Mac socket. No OpenClaw node or gateway RPC is involved.
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

/// The one request outstanding on the socket. Single-flight, so the reply's type
/// plus this slot are what identify the frame being answered.
enum Rpc {
    Register {
        key: String,
        sent: Instant,
        wall: SystemTime,
    },
    Heartbeat {
        sent: Instant,
        wall: SystemTime,
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
    /// Sessions that have ended and whose end the relay has not been told about.
    ended_pending: Vec<String>,
    /// Sessions whose heartbeat fields moved between heartbeats: one goes out
    /// early rather than up to ten seconds late.
    heartbeat_dirty: BTreeSet<String>,
    /// The device heartbeat cadence. A field so tests can drive many ticks fast.
    heartbeat_interval: Duration,
}

/// How long a per-session verdict (a stale generation, a relay rejection) keeps that session out
/// of rotation. Connections are long-lived now, so "for this connection" could mean hours; this
/// bounds it and lets a session that has since been repaired re-register on the same socket.
const QUARANTINE: Duration = Duration::from_secs(300);

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

impl Host {
    async fn observe_workspace(&mut self, key: &str) -> Result<()> {
        let execution = self
            .mirrors
            .get(key)
            .ok_or("missing local session")?
            .native
            .process
            .clone();
        let observed = match workspace::process_root(&execution, &self.home).await {
            Ok(cwd) => workspace::scan(&cwd, &self.home).await,
            Err(error) => Err(error),
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
        let mirror = self.mirrors.get_mut(key).ok_or("missing local session")?;
        if !mirror.registration.pending || mirror.dirty {
            mirror.registration.pending = true;
            // Durable before the first send: retry this exact generation/CAS
            // predecessor until ACK, even when discovery has already found C.
            mirror.persist(&self.dir.join(format!("{key}.json")))?;
        }
        Ok(registration(mirror))
    }
    fn observe(&mut self, update: Vec<NativeSession>) -> Result<Vec<String>> {
        let mut invalidated = vec![];
        let mut heartbeat = vec![];
        let mut reconciled = self
            .mirrors
            .values()
            .map(|m| {
                m.registration
                    .successor
                    .as_ref()
                    .unwrap_or(&m.native)
                    .clone()
            })
            .collect();
        reconcile_sessions(&mut reconciled, update);
        for mut native in reconciled {
            let key = native_key(&native);
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
                if mirror.ended_at.is_some() {
                    // The session is over: a hook or its own pid said so, and a
                    // listing cannot argue. Only a new process on the same identity
                    // (a resume) brings it back. The same process under a generation
                    // minted by an older formula is not a resume: reviving it would
                    // register an "ended" status the relay rejects (INVALID_REQUEST).
                    let resumed_codex_turn = mirror.ended_reason.as_deref() == Some("exited")
                        && native.provider == "codex"
                        && native.status == "busy"
                        && mirror.ended_at.is_some_and(|ended_at| {
                            native.transcript.as_deref().is_some_and(|path| {
                                codex::active_turn_started_after(path, ended_at)
                            })
                        });
                    if mirror.native.process == native.process {
                        if !resumed_codex_turn {
                            continue;
                        }
                        // Fence an end frame already in flight; the relay accepts this
                        // generation because previousGeneration names the ended one.
                        mirror.previous_generation = Some(mirror.generation.clone());
                        mirror.generation = uuid::Uuid::new_v4().to_string();
                    } else if mirror.generation == generation {
                        continue;
                    }
                    mirror.ended_at = None;
                    mirror.ended_reason = None;
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
                if mirror.native.restriction.as_deref() == Some("history-gap") {
                    native.restriction = Some("history-gap".into());
                }
                // Discovery cannot read the last assistant message; only a Stop hook
                // carries it, so a listing must not blank it.
                if native.last_assistant_message.is_none() {
                    native
                        .last_assistant_message
                        .clone_from(&mirror.native.last_assistant_message);
                }
                // The same process keeps its stored generation, whatever formula minted
                // it: an upgrade that changes the fingerprint must not re-register it.
                if mirror.generation != generation && mirror.native.process != native.process {
                    invalidate = true;
                    mirror.registration.successor = Some(native);
                    // The predecessor is not running and this Mac has verified nothing
                    // about the successor yet. "offline" is not a status the relay knows.
                    mirror.native.status = "unknown".into();
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
                        ended_reason: None,
                        ended_sent: false,
                        waiting: None,
                        stats: Default::default(),
                        subagents: Default::default(),
                    },
                );
            }
            if revived {
                // Its end is no longer news; whatever has not been sent must not be.
                self.ended_pending.retain(|pending| pending != &key);
            }
            let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
            let _ = capture_batch(mirror);
            let after = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
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
        Ok(invalidated)
    }

    fn live_count(&self) -> usize {
        self.mirrors
            .values()
            .filter(|m| m.ended_at.is_none())
            .count()
    }

    /// One hook datagram. Returns the key whose mirror moved, so the caller can
    /// send an early heartbeat instead of waiting out the ten-second cadence.
    fn apply_hook(&mut self, event: &lifecycle::HookEvent) -> Result<Option<String>> {
        let provider = match event.provider {
            lifecycle::Provider::Claude => "claude",
            lifecycle::Provider::Codex => "codex",
        };
        // An unknown session is not an error: a start hook wakes discovery, and the
        // 60s sweep picks up anything whose start was missed. An ended one is over.
        let Some(key) = self
            .mirrors
            .iter()
            .find(|(_, m)| {
                m.ended_at.is_none()
                    && m.native.provider == provider
                    && m.native.conversation == event.session_id
            })
            .map(|(key, _)| key.clone())
        else {
            return Ok(None);
        };
        match lifecycle::transition(event) {
            lifecycle::Transition::Status(status) => {
                let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
                let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                mirror.native.status = status.into();
                mirror.waiting = None;
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
                // Identity is the event's own tool and reason, not the rendered
                // row: the two hooks a question fires word it differently and
                // still name one prompt.
                // A held prompt is taken only into the private hook directory, and
                // only once the hook hears back does it wait for a remote answer.
                let held = event.held.clone().filter(|held| {
                    enrollment::hook_path()
                        .parent()
                        .is_some_and(|dir| approval::reply_allowed(dir, &held.reply, &held.id))
                        && approval::acknowledge(&held.reply, &held.id)
                }).map(|mut held| {
                    // Exact only if the reader gets the summary it was judged on: a
                    // datagram that dropped it for size shows nothing, so deny-only.
                    held.exact &= event.waiting_text.as_deref().is_some_and(|t| !t.trim().is_empty());
                    held
                });
                let prompt = WaitingPrompt {
                    id: event.tool_use_id.clone(),
                    tool: event.tool_name.clone(),
                    // Trimmed like `waiting_reason` trims it, so two hooks whose
                    // padding differs still name one prompt.
                    summary: redact(event.waiting_text.as_deref().unwrap_or("").trim()),
                    held,
                };
                let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
                let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                mirror.native.status = "waiting-approval".into();
                // Queued, not written through the cursor: this event is the
                // hook's, not the transcript's, and the file never carries it.
                // An open episode is not itself a reason to swallow this: a
                // question answered inside one turn can be followed by a real
                // permission prompt before any other transition clears it.
                if !mirror.waiting.as_ref().is_some_and(|open| open.is(&prompt)) {
                    mirror.queue.push(approval_row(text.as_deref().unwrap_or(""), &prompt));
                    mirror.waiting = Some(prompt);
                }
                persist_changed(&self.dir, &key, mirror, &before)?;
                Ok(Some(key))
            }
            lifecycle::Transition::Ended(reason) => {
                self.end_session(&key, reason)?;
                Ok(Some(key))
            }
            lifecycle::Transition::Release(id) => {
                // Still waiting in the terminal, just no longer answerable from here.
                let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
                let open = mirror.waiting.clone().filter(|w| w.held.as_ref().is_some_and(|h| h.id == id));
                if let Some(mut unheld) = open {
                    let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                    unheld.held = None;
                    mirror.waiting = Some(unheld.clone());
                    mirror.queue.push(approval_row("", &unheld));
                    persist_changed(&self.dir, &key, mirror, &before)?;
                    return Ok(Some(key));
                }
                Ok(None)
            }
            lifecycle::Transition::Start | lifecycle::Transition::Ignore => Ok(None),
        }
    }

    /// The one end path: a `SessionEnd` hook, or a pid that is gone.
    fn end_session(&mut self, key: &str, reason: &'static str) -> Result<()> {
        let Some(mirror) = self.mirrors.get_mut(key) else {
            return Ok(());
        };
        if mirror.ended_at.is_some() {
            return Ok(());
        }
        let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
        mirror.native.status = "ended".into();
        mirror.ended_at = Some(epoch_secs());
        mirror.ended_reason = Some(reason.into());
        persist_changed(&self.dir, key, mirror, &before)?;
        self.ended_pending.push(key.to_owned());
        Ok(())
    }

    /// The next end the relay is still owed, with the frame that reports it.
    /// Skips what it cannot or need not report: a key whose mirror has been reaped,
    /// one a resume has revived, and one that ended before the relay ever learned
    /// the session existed -- that last one is retired here, since there is nothing
    /// to tell and nobody to tell it to.
    fn ended_frame(&mut self, registered: &BTreeSet<String>) -> Result<Option<(Rpc, Value)>> {
        let Self {
            dir,
            mirrors,
            ended_pending,
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
            let Some(session) = mirror.session_id.clone() else {
                let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                mirror.ended_sent = true;
                persist_changed(dir, &key, mirror, &before)?;
                ended_pending.remove(index);
                continue;
            };
            // The end of the conversation is the last thing a session writes, and the
            // ACK for this frame is what stops it being tailed -- so report the end
            // only once there is nothing left to send, including bytes still unread on
            // disk. Only a key the upload loop still holds can drain; one that ended
            // before this connection ever registered it has no drainer to wait for.
            if registered.contains(&key) {
                let before = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
                let draining = mirror.pending.is_some()
                    || !mirror.queue.is_empty()
                    || capture_batch(mirror).is_ok_and(|batch| batch.is_some());
                persist_changed(dir, &key, mirror, &before)?;
                if draining {
                    continue;
                }
            }
            let reported = Rpc::Ended {
                key: key.clone(),
                at: ended_at,
                generation: mirror.generation.clone(),
                sent: Instant::now(),
            };
            let frame = json!({"type":"local.session.ended","sessionId":session,
                "generation":mirror.generation,
                "reason":mirror.ended_reason.as_deref().unwrap_or("exited"),
                "endedAt":ended_at});
            ended_pending.remove(index);
            return Ok(Some((reported, frame)));
        }
        Ok(None)
    }

    /// A held prompt nobody holds any more was answered in the terminal (Claude
    /// kills the hook when the human answers there) -- or the hold simply ended.
    /// Which one is unknowable here, so the prompt stays "waiting" but stops being
    /// answerable from Lightbulb; the next status hook moves the session on.
    /// Probed each tick with an empty datagram the hook ignores.
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
            if mirror.ended_at.is_some() || approval::deliver(&reply, None) {
                continue;
            }
            let Ok(before) = serde_json::to_value(&*mirror) else { continue };
            let Some(mut unheld) = mirror.waiting.clone() else { continue };
            unheld.held = None;
            mirror.queue.push(approval_row("", &unheld));
            mirror.waiting = Some(unheld);
            if persist_changed(&self.dir, key, mirror, &before).is_ok() {
                moved.push(key.clone());
            }
        }
        moved
    }

    /// Every live mirror, every tick: a process that is gone ends its session.
    fn liveness_tick(&mut self) {
        let gone: Vec<String> = self
            .mirrors
            .iter()
            .filter(|(_, m)| {
                m.ended_at.is_none()
                    && !still_running(&m.native.process)
                    // While a successor waits for `advance`, `native` is the dead
                    // predecessor and the successor is the session that is running.
                    && m.registration
                        .successor
                        .as_ref()
                        .is_none_or(|next| !still_running(&next.process))
            })
            .map(|(key, _)| key.clone())
            .collect();
        for key in gone {
            if let Err(error) = self.end_session(&key, "process-exited") {
                relay_log(&format!("{} end not recorded: {error}", short(&key)));
            }
        }
    }

    /// An ended session is kept for a day, then its mirrored history is dropped.
    /// Returns the reaped keys: a caller keeping its own per-key state (a live
    /// connection's registration map) has to drop them too, or it will index a
    /// mirror that is no longer there.
    #[must_use]
    fn reap(&mut self) -> Vec<String> {
        let cutoff = epoch_secs().saturating_sub(86_400);
        let expired: Vec<String> = self
            .mirrors
            .iter()
            .filter(|(_, m)| m.ended_at.is_some_and(|at| at < cutoff))
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
        updates: &mut tokio::sync::mpsc::Receiver<Vec<NativeSession>>,
        remote: impl std::future::Future<Output = Result<T>>,
    ) -> Result<T> {
        tokio::pin!(remote);
        loop {
            tokio::select! {
                result=&mut remote=>return result,
                update=updates.recv()=>{self.observe(update.ok_or("discovery stopped")?)?;}
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
                // Nothing about a live session is verified across a restart; a hook,
                // the pid check or the next sweep says what it really is.
                mirror.native.status = "unknown".into();
            }
            mirror.workspace.observe(
                Err("workspace not yet observed after restart".into()),
                false,
            );
            if mirror.native.restriction.as_deref() != Some("history-gap") {
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
            ended_pending: vec![],
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
        };
        // Ended mirrors are loaded so their end can still be reported, and so the
        // day-old ones are cleaned up on exactly one predictable path. No connection
        // exists yet, so there is no per-key state to prune alongside them.
        let _ = host.reap();
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
        let discovery = async move {
            // A 60s backstop, not the status source: hooks and the pid check drive
            // status now. A wake (a start hook, an enrollment change) runs it sooner,
            // but never more than once a second.
            let mut last = Instant::now() - Duration::from_secs(1);
            let mut last_failures: Vec<String> = vec![];
            loop {
                if let Some(remaining) = Duration::from_secs(1).checked_sub(last.elapsed()) {
                    tokio::time::sleep(remaining).await;
                }
                last = Instant::now();
                let (claude, codex) = tokio::join!(claude::discover(&home), codex::discover(&home));
                // One provider's broken listing must not hide the other's sessions,
                // so the pass is the union of whatever answered.
                let listings = [claude, codex];
                let providers = listings.len();
                let mut found = vec![];
                let mut failures = vec![];
                for listing in listings {
                    match listing {
                        Ok(sessions) => found.extend(sessions),
                        Err(error) => failures.push(error),
                    }
                }
                for error in &failures {
                    // A listing that failed is not evidence that anything stopped;
                    // say so once per distinct failure, not once a minute forever.
                    if !last_failures.contains(error) {
                        relay_log(&format!("discovery unavailable: {error}"));
                    }
                }
                // Every provider failed: this pass proves nothing. Keep the state we
                // have -- an empty listing would otherwise read as "all quiet".
                if failures.len() < providers && tx.send(found).await.is_err() {
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
        tokio::select! {_ = discovery=>{},_ = network=>{}}
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
        updates: &mut tokio::sync::mpsc::Receiver<Vec<NativeSession>>,
        human: &str,
    ) -> Result<()> {
        let mut registered = BTreeSet::<String>::new();
        let mut quarantined = BTreeMap::<String, Instant>::new();
        let mut pending_rpc: Option<Rpc> = None;
        let mut anchor: Option<ClockAnchor> = None;
        let mut deferred = std::collections::VecDeque::<Command>::new();
        let mut sent_batches = BTreeMap::<String, Instant>::new();
        let mut last_upload = Instant::now() - Duration::from_millis(500);
        let mut last_upload_key: Option<String> = None;
        let mut heartbeat_frames = std::collections::VecDeque::<Value>::new();
        let mut last_heartbeat = Instant::now();
        let mut last_early = Instant::now() - Duration::from_secs(1);
        let mut tick = tokio::time::interval(Duration::from_millis(100));
        let mut reaped = Instant::now();
        let mut last_probe = Instant::now();
        // Learned from the relay's own registration ACK; never assumed.
        let mut relay_reads_stats = false;
        // An end the relay has not been told about -- because it happened while the
        // socket was down, or in a previous run of this process -- is still owed to it.
        self.liveness_tick();
        let unreported: Vec<String> = self
            .mirrors
            .iter()
            .filter(|(key, m)| {
                m.ended_at.is_some() && !m.ended_sent && !self.ended_pending.contains(*key)
            })
            .map(|(key, _)| key.clone())
            .collect();
        self.ended_pending.extend(unreported);
        loop {
            if self.stop.load(Ordering::SeqCst) {
                return Ok(());
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
            if pending_rpc
                .as_ref()
                .is_some_and(|rpc| rpc.sent().elapsed() > Duration::from_secs(10))
            {
                return Err("session acknowledgment timed out".into());
            }
            if anchor.as_ref().is_some_and(|a| a.now().is_none()) {
                anchor = None;
            }
            if anchor.is_some() {
                let ready = deferred.iter().position(|cmd| {
                    self.mirrors.iter().any(|(key, m)| {
                        registered.contains(key) && m.session_id.as_deref() == Some(&cmd.session_id)
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
            if pending_rpc.is_none() {
                if let Some(key) = registration_due(&self.mirrors, &registered, &quarantined) {
                    let sent = Instant::now();
                    let wall = SystemTime::now();
                    let body = self.begin_registration(&key)?;
                    send(socket, &body).await?;
                    pending_rpc = Some(Rpc::Register { key, sent, wall });
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
                        heartbeat_frames = device_heartbeats(&self.mirrors, &registered, relay_reads_stats)?.into();
                    }
                    if let Some((reported, frame)) = self.ended_frame(&registered)? {
                        send(socket, &frame).await?;
                        pending_rpc = Some(reported);
                    } else if let Some(frame) = heartbeat_frames.pop_front() {
                        let sent = Instant::now();
                        let wall = SystemTime::now();
                        send(socket, &frame).await?;
                        pending_rpc = Some(Rpc::Heartbeat { sent, wall });
                    }
                }
            }
            if last_upload.elapsed() >= Duration::from_millis(500) {
                let mut keys: Vec<_> = self.mirrors.keys().cloned().collect();
                if let Some(index) = keys
                    .iter()
                    .position(|key| Some(key) == last_upload_key.as_ref())
                {
                    let len = keys.len();
                    keys.rotate_left((index + 1) % len);
                }
                for key in &keys {
                    let mirror = self.mirrors.get_mut(key).ok_or("missing local session")?;
                    if !registered.contains(key)
                        || sent_batches
                            .get(key)
                            .is_some_and(|at| at.elapsed() < Duration::from_secs(2))
                    {
                        continue;
                    }
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
                    persist_changed(&self.dir, key, mirror, &before)?;
                    if let Some(batch) = batch {
                        send(socket, &batch).await?;
                        sent_batches.insert(key.clone(), Instant::now());
                        last_upload = Instant::now();
                        last_upload_key = Some(key.clone());
                        break;
                    }
                }
            }
            tokio::select! {
                _ = tick.tick() => {
                    self.liveness_tick();
                    if last_probe.elapsed() >= Duration::from_secs(1) {
                        last_probe = Instant::now();
                        let moved = self.approval_tick();
                        self.heartbeat_dirty.extend(moved);
                    }
                    if reaped.elapsed() >= Duration::from_secs(60) {
                        reaped = Instant::now();
                        // This connection's own per-key state has to go with them.
                        for key in self.reap() {
                            registered.remove(&key);
                            sent_batches.remove(&key);
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
                    match event {
                        Some(event) => {
                            if let Some(key) = self.apply_hook(&event)? {self.heartbeat_dirty.insert(key);}
                        },
                        // The listener is gone; stop polling a closed channel.
                        None => self.hooks = None,
                    }
                },
                update = updates.recv() => {
                    let Some(update) = update else {return Err("discovery stopped".into());};
                    for key in self.observe(update)? {registered.remove(&key);}
                },
                incoming = socket.next() => {
                    let message = incoming.ok_or("session socket closed")?.map_err(|_| "session socket failed")?;
                    let Message::Text(text) = message else {
                        if matches!(message, Message::Close(_)) {return Err("session socket closed".into());}
                        if let Message::Ping(bytes) = message {socket.send(Message::Pong(bytes)).await.map_err(|_| "session socket failed")?;}
                        continue;
                    };
                    if text.len() > 16384 {return Err("oversized session frame".into());}
                    let body: Value = serde_json::from_str(&text).map_err(|_| "invalid session frame")?;
                    match body["type"].as_str() {
                        Some("local.session.registered") => {
                            let rpc = pending_rpc.take().ok_or("unsolicited session acknowledgment")?;
                            let Rpc::Register{key,sent,wall} = rpc else {return Err("mismatched session acknowledgment".into());};
                            if quarantine_active(&quarantined, &key) {
                                // A late ACK must never put a quarantined session back in rotation.
                                relay_log(&format!("{} ack ignored while quarantined", short(&key)));
                                continue;
                            }
                            relay_reads_stats = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "sessionStats"));
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
                            mirror.session_id = Some(sid.into());
                            mirror.subagents.relay = body["features"].as_array().is_some_and(|f| f.iter().any(|x| x == "sessionSubagents"));
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
                            if advanced {registered.remove(&key);} else {registered.insert(key.clone());}
                            let rtt = sent.elapsed().as_secs_f64();
                            deadline_now(server,0.0,rtt,SystemTime::now().duration_since(wall).ok().map(|d|d.as_secs_f64()))
                                .filter(|_|rtt <= 10.0).ok_or("stale time acknowledgment")?;
                            anchor = Some(ClockAnchor {server,round_trip:rtt,at:Instant::now(),wall:SystemTime::now()});
                            if !body["reconcile"].is_null() {
                                let request = body["reconcile"]["requestId"].as_str().ok_or("invalid reconciliation")?;
                                let generation = body["reconcile"]["generation"].as_str().ok_or("invalid reconciliation")?;
                                self.dispatch_reconcile(socket,&mut deferred,anchor.as_ref(),sid,request,generation).await?;
                            }
                        },
                        Some("local.device.heartbeat.ack") => {
                            let rpc = pending_rpc.take().ok_or("unsolicited session acknowledgment")?;
                            let Rpc::Heartbeat{sent,wall} = rpc else {return Err("mismatched session acknowledgment".into());};
                            super::snapshot::offer(&body["pickup"], &self.mirrors);
                            let server = body["serverTime"].as_u64().ok_or("missing server time")?;
                            let rtt = sent.elapsed().as_secs_f64();
                            deadline_now(server,0.0,rtt,SystemTime::now().duration_since(wall).ok().map(|d|d.as_secs_f64()))
                                .filter(|_|rtt <= 10.0).ok_or("stale time acknowledgment")?;
                            anchor = Some(ClockAnchor {server,round_trip:rtt,at:Instant::now(),wall:SystemTime::now()});
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
                                self.dispatch_reconcile(socket,&mut deferred,anchor.as_ref(),sid,request,generation).await?;
                            }
                        },
                        Some("local.session.ended.ack") => {
                            let rpc = pending_rpc.take().ok_or("unsolicited session acknowledgment")?;
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
                            let before=serde_json::to_value(&*mirror).map_err(|e|e.to_string())?;
                            mirror.ended_sent = true;
                            persist_changed(&self.dir,&key,mirror,&before)?;
                        },
                        Some("local.session.events.ack") => {
                            let (key,mirror) = self.mirrors.iter_mut().find(|(_,m)|m.session_id.as_deref() == body["sessionId"].as_str()).ok_or("unknown upload acknowledgment")?;
                            let batch_id = body["batchId"].as_str().ok_or("missing batch ID")?;
                            let after = body["afterSeq"].as_u64().ok_or("missing cursor")?;
                            if mirror.acknowledge(batch_id, after)? {
                                if mirror.advance() {registered.remove(key);}
                                mirror.persist(&self.dir.join(format!("{key}.json")))?;
                                sent_batches.remove(key);
                            } else {
                                relay_log(&format!("{} ignored a stale upload ack at {after}", short(key)));
                            }
                        },
                        Some("local.session.error") => {
                            let code = body["code"].as_str().unwrap_or("?").to_owned();
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
                            // own reply, and holding the slot would stall until the 10s timeout.
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
                        _ => return Err("unsupported session frame".into()),
                    }
                }
            }
        }
    }

    async fn verified(&self, native: &NativeSession) -> Result<NativeSession> {
        if native.status == "ended" || native.restriction.is_some() {
            return Err("control unavailable".into());
        }
        let program =
            provider_program(&self.home, &native.provider).ok_or("runtime unavailable")?;
        let current = if native.provider == "claude" {
            claude::capture(
                &self.home,
                &program,
                &json!({"kind":if native.background {"background"} else {"interactive"},"pid":native.process.pid,
                "sessionId":native.conversation,"status":"unknown"}),
            )
            .await?
        } else {
            codex::capture(&self.home, &program, native.process.pid)
                .await?
                .into_iter()
                .find(|s| s.conversation == native.conversation)
                .ok_or("native identity changed")?
        };
        if current.restriction.is_some()
            || current.process != native.process
            || current.conversation != native.conversation
            || current.profile != native.profile
            || current.transcript != native.transcript
            || current.socket != native.socket
            || current.socket_inode != native.socket_inode
            || current.version != native.version
        {
            return Err("native identity changed".into());
        }
        Ok(current)
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
        session: &str,
        request: &str,
        generation: &str,
    ) -> Result<()> {
        let buffered = deferred
            .iter()
            .position(|cmd| cmd.session_id == session && cmd.request_id == request);
        let receipt = match buffered.and_then(|index| deferred.remove(index)) {
            Some(cmd) => self.command(&cmd, anchor).await?,
            None => self.reconcile(session, request, generation).await?,
        };
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
                    if let Ok(Some(id)) = claude::admission(ingress) {
                        receipt = journal.finish(session, request, Some(id), false)?;
                    }
                }
            }
        }
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
        Ok(receipt)
    }

    async fn command(&mut self, cmd: &Command, anchor: Option<&ClockAnchor>) -> Result<Receipt> {
        let journal = Journal {
            dir: self.dir.join("receipts"),
        };
        private_dir(&journal.dir)?;
        if cmd.kind == "approve" {
            return self.approve(&journal, cmd, anchor).await;
        }
        if journal
            .path(&cmd.session_id, &cmd.request_id)
            .try_exists()
            .map_err(|_| "journal unavailable")?
        {
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
        self.observe_workspace(&key).await?;
        let Some(mirror) = self
            .mirrors
            .values()
            .find(|m| m.session_id.as_deref() == Some(&cmd.session_id))
        else {
            return Err("unknown command session".into());
        };
        let eligible = mirror.generation == cmd.expected_generation
            && mirror.registration.successor.is_none()
            && !self.stop.load(Ordering::SeqCst)
            && anchor
                .and_then(ClockAnchor::now)
                .is_some_and(|now| cmd.authorized_at(now));
        let native = if eligible {
            self.verified(&mirror.native).await.ok()
        } else {
            None
        };
        let Some(native) = native else {
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        };
        let mut socket = if native.provider == "claude" {
            match claude::connect(&native).await {
                Ok(socket) => Some(socket),
                Err(_) => {
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
        let now = anchor.and_then(ClockAnchor::now).unwrap_or(u64::MAX);
        if !journal.admit(cmd, &mirror.generation, now)? {
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        // Provenance is server-owned. JSON escaping keeps IDs from spoofing this boundary.
        let text = format!(
            "{}{}{}",
            cmd.header(),
            mirror.workspace.notice(),
            cmd.text.as_deref().unwrap_or("")
        );
        let anchored = journal.anchor(
            cmd,
            &native,
            text.clone(),
            Some(mirror.workspace.revision()),
        );
        if anchored.is_err()
            || self.stop.load(Ordering::SeqCst)
            || !anchor
                .and_then(ClockAnchor::now)
                .is_some_and(|now| cmd.authorized_at(now))
            || !process(native.process.pid).is_ok_and(|current| current == native.process)
        {
            return journal.finish(&cmd.session_id, &cmd.request_id, None, false);
        }
        // From this point failure/cancellation is unknown, never a retry/rejection.
        if let Some(socket) = &mut socket {
            let from = claude::sender_handle(cmd.label.as_ref().and_then(|l| l.handle.as_deref()));
            let _ = claude::send(socket, &text, &from).await;
            let anchored = journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation)?;
            if let Some(ingress) = &anchored.ingress {
                if let Some(id) = claude::await_admission(ingress).await {
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
            .and_then(|w| w.held.clone())
            .filter(|held| answerable(held, cmd));
        let live = mirror.generation == cmd.expected_generation
            && mirror.registration.successor.is_none()
            && mirror.ended_at.is_none()
            && !self.stop.load(Ordering::SeqCst)
            && cmd.authorized_at(now)
            && self.verified(&mirror.native).await.is_ok();
        let mirror = &self.mirrors[&key];
        if !journal.admit(cmd, &mirror.generation, now)? {
            return journal.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation);
        }
        let confined = |held: &approval::Held| {
            enrollment::hook_path()
                .parent()
                .is_some_and(|dir| approval::reply_allowed(dir, &held.reply, &held.id))
        };
        let Some(held) = held.filter(|held| live && confined(held)) else {
            return journal.finish(&cmd.session_id, &cmd.request_id, None, false);
        };
        let answer = approval::Answer {
            approval_id: held.id.clone(),
            input_digest: held.input_digest.clone(),
            decision: cmd.decision.clone().unwrap_or_default(),
            mode: cmd.mode.clone(),
            message: (cmd.decision.as_deref() == Some("deny")).then(|| {
                match cmd.text.as_deref().map(str::trim).filter(|t| !t.is_empty()) {
                    Some(reason) => format!("Denied from Lightbulb: {reason}"),
                    None => "Denied from Lightbulb.".to_owned(),
                }
            }),
        };
        if !approval::deliver(&held.reply, Some(&answer)) {
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

/// Does this command answer exactly this held prompt, with an option it offered?
fn answerable(held: &approval::Held, cmd: &Command) -> bool {
    // A person's decision only: an agent never answers a permission prompt.
    cmd.actor.is_none()
        && cmd.actor_user_id.as_deref().is_some_and(|u| !u.is_empty())
        && cmd.approval_id.as_deref() == Some(held.id.as_str())
        && cmd.input_digest.as_deref() == Some(held.input_digest.as_str())
        && match cmd.decision.as_deref() {
            Some("allow") => held.exact && cmd.mode.as_ref().is_none_or(|m| held.modes.contains(m)),
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
            "exact": held.exact,
            "modes": held.modes,
        },
    });
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
        "outcome":receipt.outcome,"providerMessageId":receipt.provider_message_id,"held":receipt.held})
}

fn native_key(native: &NativeSession) -> String {
    fingerprint(&json!([native.provider, native.profile, native.conversation]).to_string())
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

/// A read failure is a history problem only. A process that is gone is ended by
/// `liveness_tick`, which is the single owner of that verdict.
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
        "providerVersion":mirror.native.version,"status":mirror.native.status,"activeTurnId":mirror.native.active_turn,"controlUnavailableReason":mirror.native.restriction,
        "title":mirror.native.title,"cwdName":mirror.native.cwd_name,
        "workspace":mirror.workspace.summary()}});
    // Omitted for a terminal session, so a backend that predates background jobs keeps accepting it.
    if mirror.native.background {
        body["session"]["kind"] = json!("background");
    }
    if let Some(previous) = &mirror.previous_generation {
        body["previousGeneration"] = json!(previous);
    }
    body
}

pub(super) fn relay_log(msg: &str) {
    crate::log(&format!("[local-sessions] {msg}"));
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
    mirrors
        .iter()
        .find(|(key, mirror)| {
            mirror.ended_at.is_none()
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
    let envelope = |sessions: &[Value]| json!({"type":"local.device.heartbeat","sessions":sessions});
    let mut frames = vec![];
    let mut sessions: Vec<Value> = vec![];
    for (key, m) in mirrors.iter().filter(|(key, m)| {
        registered.contains(*key) && m.ended_at.is_none() && m.session_id.is_some()
    }) {
        let entry = json!({"sessionId":m.session_id,"generation":m.generation,
            "status":m.native.status,"activeTurnId":m.native.active_turn,
            "controlUnavailableReason":m.native.restriction,
            "title":m.native.title,"cwdName":m.native.cwd_name,
            "lastAssistantMessage":m.native.last_assistant_message,
            "workspace":m.workspace.summary()});
        // Only to a relay that said it reads them: an older one rejects the whole frame.
        let mut entry = entry;
        if let Some(stats) = m.stats.wire().filter(|_| with_stats) {
            entry["stats"] = stats;
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
mod tests {
    use super::*;

    const SID: &str = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";
    const SID_B: &str = "bbbbcccc-dddd-4eee-8fff-000011112222";

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
            background: false,
        }
    }

    fn test_mirror(conversation: &str, generation: &str) -> Mirror {
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
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
        }
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
            ended_pending: vec![],
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
        }
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
    fn local_sessions_background_job_registers_its_kind_and_a_terminal_session_omits_it() {
        let mut mirror = test_mirror(SID, "gen-1");
        assert!(
            registration(&mirror)["session"].get("kind").is_none(),
            "a backend that predates background jobs rejects an unknown key"
        );
        let mut persisted = serde_json::to_value(&mirror.native).unwrap();
        persisted.as_object_mut().unwrap().remove("background");
        assert!(
            !serde_json::from_value::<NativeSession>(persisted)
                .unwrap()
                .background
        );
        mirror.native.background = true;
        assert_eq!(registration(&mirror)["session"]["kind"], "background");
        mirror.session_id = Some(SID.into());
        let frames = device_heartbeats(
            &BTreeMap::from([(SID.to_string(), mirror)]),
            &BTreeSet::from([SID.to_string()]),
            false,
        )
        .unwrap();
        assert!(
            frames[0]["sessions"][0].get("kind").is_none(),
            "the heartbeat entry never carries it"
        );
    }

    #[test]
    fn local_sessions_hook_events_drive_status_and_end() {
        let dir = tempfile::tempdir().unwrap();
        let mut host = test_host(dir.path(), vec![(SID, test_mirror(SID, "gen-1"))]);
        let key = host.mirrors.keys().next().unwrap().clone();
        let mut ev = lifecycle::HookEvent {
            provider: lifecycle::Provider::Codex,
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
        assert_eq!(host.reap(), vec![key.clone()]);
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
        let old_generation = native.process.generation();
        let mut mirror = test_mirror(SID, &old_generation);
        mirror.native.status = "ended".into();
        mirror.session_id = Some("session-id".into());
        mirror.registration.acknowledged = Some(old_generation.clone());
        mirror.ended_at = Some(ended_at);
        mirror.ended_reason = Some("exited".into());
        mirror.ended_sent = true;
        let mut host = test_host(dir.path(), vec![(SID, mirror)]);
        host.ended_pending.push(SID.into());

        assert_eq!(host.observe(vec![native]).unwrap(), vec![SID]);
        let resumed = &host.mirrors[SID];
        assert!(resumed.ended_at.is_none());
        assert!(resumed.ended_reason.is_none());
        assert_eq!(resumed.native.status, "busy");
        assert_ne!(resumed.generation, old_generation);
        assert_eq!(resumed.previous_generation.as_deref(), Some(old_generation.as_str()));
        assert!(host.ended_pending.is_empty());
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
        for key in host.reap() {
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
    fn local_sessions_held_prompt_is_tagged_acknowledged_and_released_when_answered_in_the_terminal() {
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
            modes: vec![],
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
        // Answered in the terminal (Claude kills the hook) or let go: the channel is
        // gone. Still waiting as far as we know, but no longer answerable here.
        drop(hook_side);
        let _ = fs::remove_file(&reply);
        assert_eq!(host.approval_tick(), vec![key.clone()]);
        assert_eq!(host.mirrors[&key].native.status, "waiting-approval");
        assert!(host.mirrors[&key].waiting.as_ref().unwrap().held.is_none());
        assert!(host.mirrors[&key].queue[1].get("itemId").is_none());
        assert!(host.approval_tick().is_empty(), "once, not every tick");
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
        let held = approval::Held { id: id.clone(), input_digest: "d".repeat(64), reply: reply.to_string_lossy().into_owned(), exact: true, modes: vec![] };
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
        assert_eq!(host.apply_hook(&release).unwrap(), Some(key.clone()));
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
            modes: vec!["acceptEdits".into()],
        };
        assert!(answerable(&held, &cmd));
        assert!(!answerable(&approval::Held { exact: false, ..held.clone() }, &cmd), "inexact is deny-only");
        assert!(!answerable(&approval::Held { input_digest: "e".repeat(64), ..held.clone() }, &cmd));
        let mut deny = cmd.clone();
        deny.decision = Some("deny".into());
        assert!(answerable(&approval::Held { exact: false, ..held.clone() }, &deny));
        let mut widened = cmd.clone();
        widened.mode = Some("plan".into());
        assert!(!answerable(&held, &widened), "only a mode the prompt offered");
        let mut agent: Command = serde_json::from_value(json!({"requestId":"r2","expectedGeneration":"g1","kind":"approve",
            "approvalId":held.id,"decision":"allow","inputDigest":held.input_digest,"expectedTurnId":null,"text":null,
            "sessionId":"s","roomId":"room","actor":{"kind":"agent","id":"a1"},"invocationExpiresAt":2000,
            "commandSeq":2,"state":"dispatched","createdAt":1000,"deliverBy":1030,"held":false,"providerMessageId":null})).unwrap();
        assert!(!answerable(&held, &agent), "an agent never answers a prompt");
        agent.actor = None;
        assert!(!answerable(&held, &agent), "nor does a command with no person behind it");
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
            background: false,
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
            tx.send(vec![]).await.unwrap();
            tx.send(vec![]).await.unwrap();
            tx.send(vec![native]).await.unwrap();
            tx.send(vec![]).await.unwrap();
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
        assert_eq!(
            stored.native.status, "unknown",
            "an empty listing never downgrades a session"
        );
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
        let native = observed_fixture(dir.path());
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
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
        };
        let mut host = Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.path().into(),
            home,
            mirrors: BTreeMap::from([("owned".into(), mirror)]),
            stop: Arc::new(AtomicBool::new(false)),
            hooks: None,
            ended_pending: vec![],
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
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
            background: false,
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
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
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
            background: false,
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
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
        };
        let mut host = Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.path().into(),
            home: dir.path().into(),
            mirrors: BTreeMap::from([("local".into(), mirror)]),
            stop: Arc::new(AtomicBool::new(false)),
            hooks: None,
            ended_pending: vec![],
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
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
        tx.send(vec![native]).await.unwrap();
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
            // this error is. Leaving the RPC slot occupied would stall until the 10s timeout.
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
            ended_pending: vec![],
            heartbeat_dirty: BTreeSet::new(),
            heartbeat_interval: Duration::from_secs(10),
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

    fn frame_keys(value: &Value) -> BTreeSet<&str> {
        value
            .as_object()
            .expect("frame object")
            .keys()
            .map(String::as_str)
            .collect()
    }

    fn hook(provider: lifecycle::Provider, event: &str, session: &str) -> lifecycle::HookEvent {
        lifecycle::HookEvent {
            provider,
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
                    "generation":generation,"afterSeq":0,"serverTime":1000,"reconcile":null})).await;
            }
            // One frame for the device, not one per session.
            let hb = next_frame(&mut socket).await;
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
                "reconcile":[],"serverTime":1010});
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
            tx.send(vec![resumed]).await.unwrap();
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
        // Nothing to read yet: the bytes arrive only once the session is registered,
        // so no upload pass can have carried them away before it ends.
        let transcript = mirror.native.transcript.clone().unwrap();
        fs::write(&transcript, "").unwrap();
        let mut host = test_host(dir.path(), vec![("a-session", mirror)]);
        host.heartbeat_interval = Duration::from_secs(60);
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
            hooks_tx
                .send(hook(lifecycle::Provider::Claude, "SessionEnd", "s"))
                .await
                .unwrap();
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
            tx.send(vec![resumed]).await.unwrap();
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
