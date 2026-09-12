//! Dedicated paired-Mac socket. No OpenClaw node or gateway RPC is involved.
use super::*;
use futures_util::{SinkExt, StreamExt};
use std::{
    collections::BTreeMap,
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

pub(super) struct Host {
    workspace_at: Instant,
    workspace_key: Option<String>,
    dir: PathBuf,
    home: PathBuf,
    mirrors: BTreeMap<String, Mirror>,
    stop: Arc<AtomicBool>,
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
                        && m.native.status != "offline")
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
            let generation =
                fingerprint(&serde_json::to_string(&native.process).map_err(|e| e.to_string())?);
            if let Some(mirror) = self.mirrors.get_mut(&key) {
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
                if mirror.generation != generation {
                    mirror.registration.successor = Some(native);
                    mirror.native.status = "offline".into();
                    if mirror.native.restriction.as_deref() != Some("history-gap") {
                        mirror.native.restriction = Some("runtime-unverified".into());
                    }
                    mirror.advance();
                } else {
                    mirror.registration.successor = None;
                    mirror.native = native;
                }
            } else {
                if self.mirrors.len() >= 512 {
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
                    },
                );
            }
            let mirror = self.mirrors.get_mut(&key).ok_or("missing local session")?;
            let _ = capture_batch(mirror);
            let after = serde_json::to_value(&*mirror).map_err(|e| e.to_string())?;
            if before.as_ref() != Some(&after) || mirror.dirty {
                if before.as_ref().is_none_or(|b| {
                    b["native"] != after["native"] || b["generation"] != after["generation"]
                }) {
                    invalidated.push(key.clone());
                }
                mirror.persist(&self.dir.join(format!("{key}.json")))?;
            }
        }
        Ok(invalidated)
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
        let mut mirrors = BTreeMap::new();
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
            mirror.native.status = "offline".into();
            mirror.workspace.observe(
                Err("workspace not yet observed after restart".into()),
                false,
            );
            if mirror.native.restriction.as_deref() != Some("history-gap") {
                mirror.native.restriction = Some("runtime-unverified".into());
            }
            if let Some(successor) = &mut mirror.registration.successor {
                successor.status = "offline".into();
                if successor.restriction.as_deref() != Some("history-gap") {
                    successor.restriction = Some("runtime-unverified".into());
                }
            }
            // The offline downgrade above is new RAM state, not yet a durable replay.
            mirror.dirty = true;
            if mirrors.len() >= 512 {
                return Err("local session capacity reached".into());
            }
            mirrors.insert(key.into(), mirror);
        }
        Ok(Self {
            workspace_at: Instant::now() - Duration::from_secs(30),
            workspace_key: None,
            dir,
            home,
            mirrors,
            stop,
        })
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
            loop {
                let (claude, codex) = tokio::join!(claude::discover(&home), codex::discover(&home));
                let mut found = claude.unwrap_or_default();
                found.extend(codex.unwrap_or_default());
                if tx.send(found).await.is_err() {
                    break;
                }
                tokio::select! {_ = tokio::time::sleep(Duration::from_secs(2))=>{},_ = wake.notified()=>{}}
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
                    let outcome = self.connection(&mut socket, &mut rx).await;
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
                if self
                    .observing_wait(&mut rx, async {
                        tokio::time::sleep(Duration::from_secs(delay)).await;
                        Ok(())
                    })
                    .await
                    .is_err()
                {
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
    ) {
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
    ) -> Result<()> {
        let mut registered = BTreeMap::<String, Instant>::new();
        let mut quarantined = std::collections::BTreeSet::<String>::new();
        let mut pending_rpc: Option<(String, bool, Instant, SystemTime, Value)> = None;
        let mut anchor: Option<ClockAnchor> = None;
        let mut deferred = std::collections::VecDeque::<Command>::new();
        let mut sent_batches = BTreeMap::<String, Instant>::new();
        let mut last_upload = Instant::now() - Duration::from_millis(500);
        let mut last_upload_key: Option<String> = None;
        let mut tick = tokio::time::interval(Duration::from_millis(100));
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
                    .filter(|(_, m)| m.native.status != "offline")
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
                .is_some_and(|(_, _, at, _, _)| at.elapsed() > Duration::from_secs(10))
            {
                return Err("session acknowledgment timed out".into());
            }
            if anchor.as_ref().is_some_and(|a| a.now().is_none()) {
                anchor = None;
            }
            if anchor.is_some() {
                let ready = deferred.iter().position(|cmd| {
                    self.mirrors.iter().any(|(key, m)| {
                        registered.contains_key(key)
                            && m.session_id.as_deref() == Some(&cmd.session_id)
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
                if let Some(key) = self
                    .mirrors
                    .keys()
                    .find(|key| !registered.contains_key(*key) && !quarantined.contains(*key))
                    .cloned()
                {
                    let sent = Instant::now();
                    let wall = SystemTime::now();
                    let body = self.begin_registration(&key)?;
                    send(socket, &body).await?;
                    let metadata = serde_json::to_value(&self.mirrors[&key].native)
                        .map_err(|e| e.to_string())?;
                    pending_rpc = Some((key, true, sent, wall, metadata));
                } else if let Some((key, _)) = registered
                    .iter()
                    .find(|(_, at)| at.elapsed() >= Duration::from_secs(10))
                {
                    let mirror = &self.mirrors[key];
                    let sent = Instant::now();
                    let wall = SystemTime::now();
                    send(socket, &json!({"type":"local.session.heartbeat", "sessionId":mirror.session_id,"generation":mirror.generation,
                        "status":mirror.native.status,"activeTurnId":mirror.native.active_turn,"controlUnavailableReason":mirror.native.restriction,
                        "title":mirror.native.title,"cwdName":mirror.native.cwd_name,
                        "workspace":mirror.workspace.summary()})).await?;
                    pending_rpc = Some((
                        key.clone(),
                        false,
                        sent,
                        wall,
                        serde_json::to_value(&mirror.native).map_err(|e| e.to_string())?,
                    ));
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
                    if !registered.contains_key(key)
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
                _ = tick.tick() => {},
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
                        Some("local.session.registered" | "local.session.heartbeat.ack") => {
                            let (key,is_register,sent,wall,metadata) = pending_rpc.take().ok_or("unsolicited session acknowledgment")?;
                            if is_register != (body["type"] == "local.session.registered") {return Err("mismatched session acknowledgment".into());}
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
                            if is_register {
                                mirror.registered();
                                relay_log(&format!("{} registered as {} at cursor {after}", short(&key), short(sid)));
                            }
                            let advanced=mirror.advance();
                            persist_changed(&self.dir,&key,mirror,&before)?;
                            // An ACK for live B cannot re-enable B after discovery
                            // has downgraded it or found C while the RPC was in flight.
                            if !advanced && metadata == serde_json::to_value(&mirror.native).map_err(|e|e.to_string())? {
                                registered.insert(key.clone(),Instant::now());
                            } else {registered.remove(&key);}
                            let rtt = sent.elapsed().as_secs_f64();
                            deadline_now(server,0.0,rtt,SystemTime::now().duration_since(wall).ok().map(|d|d.as_secs_f64()))
                                .filter(|_|rtt <= 10.0).ok_or("stale time acknowledgment")?;
                            anchor = Some(ClockAnchor {server,round_trip:rtt,at:Instant::now(),wall:SystemTime::now()});
                            if !body["reconcile"].is_null() {
                                let request = body["reconcile"]["requestId"].as_str().ok_or("invalid reconciliation")?;
                                let generation = body["reconcile"]["generation"].as_str().ok_or("invalid reconciliation")?;
                                // Dispatch precedes registration ACK on the real relay.
                                // The matching bounded pre-ACK frame is already here;
                                // admit it before considering a missing-journal tombstone.
                                let buffered = deferred.iter().position(|cmd|cmd.session_id == sid && cmd.request_id == request);
                                let receipt = if let Some(cmd) = buffered.and_then(|index|deferred.remove(index)) {
                                    self.command(&cmd,anchor.as_ref()).await?
                                } else {self.reconcile(sid,request,generation).await?};
                                send(socket,&receipt_wire(sid,request,&receipt)).await?;
                            }
                        },
                        Some("local.session.events.ack") => {
                            let (key,mirror) = self.mirrors.iter_mut().find(|(_,m)|m.session_id.as_deref() == body["sessionId"].as_str()).ok_or("unknown upload acknowledgment")?;
                            mirror.acknowledge(body["batchId"].as_str().ok_or("missing batch ID")?,body["afterSeq"].as_u64().ok_or("missing cursor")?)?;
                            if mirror.advance() {registered.remove(key);}
                            mirror.persist(&self.dir.join(format!("{key}.json")))?;
                            sent_batches.remove(key);
                        },
                        Some("local.session.error") => {
                            let code = body["code"].as_str().unwrap_or("?").to_owned();
                            let named = body["sessionId"].as_str().and_then(|sid| {
                                self.mirrors.iter().find(|(_, m)| m.session_id.as_deref() == Some(sid)).map(|(k, _)| k.clone())
                            });
                            let inflight = pending_rpc.as_ref().map(|(k, ..)| k.clone());
                            let Some(key) = named.or(inflight) else {
                                relay_log(&format!("relay rejected an unattributable frame: {code}"));
                                return Err("local session relay rejected the frame".into());
                            };
                            if pending_rpc.as_ref().is_some_and(|(k, ..)| *k == key) {
                                pending_rpc = None;
                            }
                            quarantine(&mut quarantined, &mut registered, &mut sent_batches, &key, &format!("relay rejected a frame: {code}"));
                        },
                        Some("local.session.command") => {
                            let command = parse_command(&body)?;
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
        if native.status == "offline" || native.restriction.is_some() {
            return Err("control unavailable".into());
        }
        let program =
            provider_program(&self.home, &native.provider).ok_or("runtime unavailable")?;
        let current = if native.provider == "claude" {
            claude::capture(
                &self.home,
                &program,
                &json!({"kind":"interactive","pid":native.process.pid,
                "sessionId":native.conversation,"status":"unknown"}),
            )
            .await?
        } else {
            codex::capture(&self.home, &program, native.process.pid).await?
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
        } else if let Ok(Some(id)) = codex::send(&native, &text).await {
            return journal.finish(&cmd.session_id, &cmd.request_id, Some(id), true);
        }
        self.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation)
            .await
    }
}

fn parse_command(body: &Value) -> Result<Command> {
    let mut command: Command =
        serde_json::from_value(body["command"].clone()).map_err(|_| "invalid native command")?;
    command.label = serde_json::from_value::<ActorLabel>(body["actor"].clone()).ok();
    Ok(command)
}

fn receipt_wire(session: &str, request: &str, receipt: &Receipt) -> Value {
    json!({"type":"local.session.receipt","sessionId":session,"requestId":request,"generation":receipt.generation,
        "outcome":receipt.outcome,"providerMessageId":receipt.provider_message_id,"held":receipt.held})
}

fn native_key(native: &NativeSession) -> String {
    fingerprint(&json!([native.provider, native.profile, native.conversation]).to_string())
}

fn capture_batch(mirror: &mut Mirror) -> Result<Option<Value>> {
    mirror.next_batch().inspect_err(|reason| {
        if process(mirror.native.process.pid).is_err() {
            mirror.native.status = "offline".into();
        }
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

fn quarantine(
    quarantined: &mut std::collections::BTreeSet<String>,
    registered: &mut BTreeMap<String, Instant>,
    sent_batches: &mut BTreeMap<String, Instant>,
    key: &str,
    reason: &str,
) {
    relay_log(&format!("{} quarantined for this connection: {reason}", short(key)));
    quarantined.insert(key.to_owned());
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
    fn local_sessions_failed_registration_write_retries_before_send_and_survives_lost_ack() {
        let dir = tempfile::tempdir().unwrap();
        let scope = dir.path().join("scope");
        let mut a = observed_fixture(dir.path());
        a.transcript = None;
        a.status = "offline".into();
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
        assert_eq!(replay["session"]["status"], "offline");
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
    fn local_sessions_command_frame_label_is_optional_and_never_trusted_for_identity() {
        let frame = json!({"type":"local.session.command","command":{"requestId":"r1","expectedGeneration":"g1","kind":"send","text":"hello",
            "expectedTurnId":null,"sessionId":"s1","roomId":"room","actorUserId":"alice","commandSeq":1,"state":"dispatched","createdAt":100,"deliverBy":130,"held":false,"providerMessageId":null},
            "actor":{"kind":"human","handle":"aden","displayName":"Aden Kim"}});
        let cmd = parse_command(&frame).unwrap();
        assert_eq!(
            cmd.label.as_ref().and_then(|l| l.handle.as_deref()),
            Some("aden")
        );
        assert_eq!(cmd.actor_user_id.as_deref(), Some("alice"));
        let mut bare = frame.clone();
        bare.as_object_mut().unwrap().remove("actor");
        assert!(parse_command(&bare).unwrap().label.is_none());
        let mut forged = frame;
        forged["actor"] = json!("not-an-object");
        assert!(parse_command(&forged).unwrap().label.is_none());
    }
    #[test]
    fn local_sessions_failed_observation_write_retries_identical_snapshot() {
        for completed in [false, true] {
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
            if completed {
                host.observe(vec![native.clone()]).unwrap();
            }
            let update = if completed { vec![] } else { vec![native] };
            host.dir = dir.path().join("unavailable");
            assert!(host.observe(update.clone()).is_err());
            host.dir = scope.clone();
            host.observe(update).unwrap();
            let stored: Mirror = private_read(&scope.join(format!("{key}.json")))
                .expect("identical observation must retry the failed write");
            assert_eq!(
                stored.native.status,
                if completed { "offline" } else { "unknown" }
            );
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
            a.status = "offline".into();
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
            assert_eq!(retry["session"]["status"], "offline");
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
        assert_eq!(stored.native.status, "offline");
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
    fn local_sessions_observation_does_not_rewrite_unchanged_idle_or_completed_state() {
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
        host.observe(vec![]).unwrap();
        let completed = fs::metadata(&path).unwrap().ino();
        assert_ne!(completed, inode);
        for _ in 0..10 {
            host.observe(vec![]).unwrap();
        }
        assert_eq!(fs::metadata(path).unwrap().ino(), completed);
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
            codex::capture(&home, &program, pid).await.unwrap()
        };
        assert_eq!(native.conversation, conversation);
        assert_eq!(native.restriction, None);
        let generation = fingerprint(&serde_json::to_string(&native.process).unwrap());
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
                    "local.session.register" | "local.session.heartbeat" => {
                        let registered = body["type"] == "local.session.register";
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
                        socket.send(Message::Text(json!({"type":if registered {"local.session.registered"} else {"local.session.heartbeat.ack"},
                            "sessionId":sid,"generation":expected_generation,"afterSeq":after,"serverTime":now,
                            "reconcile":if sent && !admitted {json!({"requestId":request,"generation":expected_generation,"state":"unknown"})} else {Value::Null}}).to_string().into())).await.unwrap();
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
        };
        let mut host = Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.path().into(),
            home,
            mirrors: BTreeMap::from([("owned".into(), mirror)]),
            stop: Arc::new(AtomicBool::new(false)),
        };
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = host.connection(&mut socket, &mut rx).await;
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
        };
        private_write(&dir.path().join(format!("{key}.json")), &mirror).unwrap();
        let host = Host::restore(
            dir.path().into(),
            dir.path().join("no-provider-or-node-binary"),
            Arc::new(AtomicBool::new(false)),
        )
        .unwrap();
        assert_eq!(host.mirrors[&key].pending, mirror.pending);
        assert_eq!(host.mirrors[&key].native.status, "offline");
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
        };
        let mut host = Host {
            workspace_at: Instant::now(),
            workspace_key: None,
            dir: dir.path().into(),
            home: dir.path().into(),
            mirrors: BTreeMap::from([("local".into(), mirror)]),
            stop: Arc::new(AtomicBool::new(false)),
        };
        let (_tx, mut rx) = tokio::sync::mpsc::channel(1);
        let (mut socket, _) = tokio_tungstenite::connect_async(format!("ws://{address}"))
            .await
            .unwrap();
        let _ = host.connection(&mut socket, &mut rx).await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 1);
        assert!(stored.pending.is_none());
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
        let _ = host.connection(&mut socket, &mut rx).await;
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
        let _ = host.connection(&mut socket, &mut rx).await;
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
        let _ = host.connection(&mut socket, &mut rx).await;
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
        let _ = host.connection(&mut socket, &mut rx).await;
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
        let _ = host.connection(&mut socket, &mut rx).await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert!(stored.pending.is_some(), "the rejected batch stays pending for the next connection's reconciliation");
        assert_eq!(stored.after_seq, 0);
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
        tokio::time::timeout(Duration::from_secs(8), host.supervise(enrollment, identity, wake, status.clone()))
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
        let _ = host.connection(&mut socket, &mut rx).await;
        server.await.unwrap();
        let stored: Mirror = private_read(&dir.path().join("local.json")).unwrap();
        assert_eq!(stored.after_seq, 380);
        assert!(stored.pending.is_none() && stored.queue.is_empty());
    }
}
