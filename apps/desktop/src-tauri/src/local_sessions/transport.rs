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
    dir: PathBuf,
    home: PathBuf,
    mirrors: BTreeMap<String, Mirror>,
    stop: Arc<AtomicBool>,
}

impl Host {
    fn begin_registration(&mut self, key: &str) -> Result<Value> {
        let mirror = self.mirrors.get_mut(key).ok_or("missing local session")?;
        if !mirror.registration.pending {
            mirror.registration.pending = true;
            // Durable before the first send: retry this exact generation/CAS
            // predecessor until ACK, even when discovery has already found C.
            private_write(&self.dir.join(format!("{key}.json")), mirror)?;
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
            if before.as_ref() != Some(&after) {
                if before.as_ref().is_none_or(|b| {
                    b["native"] != after["native"] || b["generation"] != after["generation"]
                }) {
                    invalidated.push(key.clone());
                }
                private_write(&self.dir.join(format!("{key}.json")), mirror)?;
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
            if mirror.native.restriction.as_deref() != Some("history-gap") {
                mirror.native.restriction = Some("runtime-unverified".into());
            }
            if let Some(successor) = &mut mirror.registration.successor {
                successor.status = "offline".into();
                if successor.restriction.as_deref() != Some("history-gap") {
                    successor.restriction = Some("runtime-unverified".into());
                }
            }
            if mirrors.len() >= 512 {
                return Err("local session capacity reached".into());
            }
            mirrors.insert(key.into(), mirror);
        }
        Ok(Self {
            dir,
            home,
            mirrors,
            stop,
        })
    }

    pub(super) async fn supervise(
        mut self,
        enrollment: Enrollment,
        identity: crate::keychain::DeviceIdentity,
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
                    delay = 1;
                    self.connection(&mut socket, &mut rx).await
                }
                .await;
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
                delay = (delay * 2).min(30);
            }
        };
        tokio::select! {_ = discovery=>{},_ = network=>{}}
    }
    async fn connection(
        &mut self,
        socket: &mut Socket,
        updates: &mut tokio::sync::mpsc::Receiver<Vec<NativeSession>>,
    ) -> Result<()> {
        let mut registered = BTreeMap::<String, Instant>::new();
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
                    .find(|key| !registered.contains_key(*key))
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
                        "status":mirror.native.status,"activeTurnId":null,"controlUnavailableReason":mirror.native.restriction})).await?;
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
                        Err(_) => {
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
                            let mirror = self.mirrors.get_mut(&key).ok_or("unknown local session")?;
                            let before=serde_json::to_value(&*mirror).map_err(|e|e.to_string())?;
                            if body["generation"] != mirror.generation {return Err("stale generation acknowledgment".into());}
                            let sid = body["sessionId"].as_str().filter(|s|uuid::Uuid::parse_str(s).is_ok()).ok_or("invalid session ID")?;
                            if mirror.session_id.as_ref().is_some_and(|known|known != sid) {return Err("session identity changed".into());}
                            let after = body["afterSeq"].as_u64().ok_or("missing upload cursor")?;
                            if after != mirror.after_seq && !(mirror.pending.is_some() && after == mirror.after_seq + 1) {return Err("upload cursor conflict".into());}
                            mirror.session_id = Some(sid.into());
                            if is_register {mirror.registered();}
                            let advanced=mirror.advance();
                            persist_changed(&self.dir,&key,mirror,&before)?;
                            // An ACK for live B cannot re-enable B after discovery
                            // has downgraded it or found C while the RPC was in flight.
                            if !advanced && metadata == serde_json::to_value(&mirror.native).map_err(|e|e.to_string())? {
                                registered.insert(key.clone(),Instant::now());
                            } else {registered.remove(&key);}
                            let rtt = sent.elapsed().as_secs_f64();
                            let server = body["serverTime"].as_u64().ok_or("missing server time")?;
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
                            private_write(&self.dir.join(format!("{key}.json")),mirror)?;
                            sent_batches.remove(key);
                        },
                        Some("local.session.error") => return Err("local session relay rejected the frame".into()),
                        Some("local.session.command") => {
                            let command: Command = serde_json::from_value(body["command"].clone()).map_err(|_|"invalid native command")?;
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

    async fn reconcile(&self, session: &str, request: &str, generation: &str) -> Result<Receipt> {
        let journal = Journal {
            dir: self.dir.join("receipts"),
        };
        private_dir(&journal.dir)?;
        let receipt = journal.reconcile(session, request, generation)?;
        if receipt.outcome == "unknown" {
            if let Some(ingress) = &receipt.ingress {
                if ingress.native.provider == "claude"
                    && self.verified(&ingress.native).await.is_ok()
                {
                    if let Ok(Some(id)) = claude::admission(ingress) {
                        return journal.finish(session, request, Some(id), false);
                    }
                }
            }
        }
        Ok(receipt)
    }

    async fn command(&self, cmd: &Command, anchor: Option<&ClockAnchor>) -> Result<Receipt> {
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
                .is_some_and(|now| now < cmd.deliver_by);
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
            "[Lightbulb teammate contribution {}]\n{}",
            json!({"actorUserId":cmd.actor_user_id,"roomId":cmd.room_id,"requestId":cmd.request_id}),
            cmd.text.as_deref().unwrap_or("")
        );
        if journal.anchor(cmd, &native, text.clone()).is_err()
            || self.stop.load(Ordering::SeqCst)
            || !anchor
                .and_then(ClockAnchor::now)
                .is_some_and(|now| now < cmd.deliver_by)
            || !process(native.process.pid).is_ok_and(|current| current == native.process)
        {
            return journal.finish(&cmd.session_id, &cmd.request_id, None, false);
        }
        // From this point failure/cancellation is unknown, never a retry/rejection.
        if let Some(socket) = &mut socket {
            let _ = claude::send(socket, &text).await;
        } else if let Ok(Some(id)) = codex::send(&native, &text).await {
            return journal.finish(&cmd.session_id, &cmd.request_id, Some(id), true);
        }
        self.reconcile(&cmd.session_id, &cmd.request_id, &cmd.expected_generation)
            .await
    }
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
fn persist_changed(dir: &Path, key: &str, mirror: &Mirror, before: &Value) -> Result<()> {
    if &serde_json::to_value(mirror).map_err(|e| e.to_string())? != before {
        private_write(&dir.join(format!("{key}.json")), mirror)?;
    }
    Ok(())
}

fn registration(mirror: &Mirror) -> Value {
    let mut body = json!({"type":"local.session.register", "session":{"provider":mirror.native.provider,
        "profileFingerprint":mirror.native.profile,"nativeConversationId":mirror.native.conversation,"generation":mirror.generation,
        "providerVersion":mirror.native.version,"status":mirror.native.status,"activeTurnId":null,"controlUnavailableReason":mirror.native.restriction}});
    if let Some(previous) = &mirror.previous_generation {
        body["previousGeneration"] = json!(previous);
    }
    body
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
        };
        let key = native_key(&native);
        let mirror = Mirror {
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
        };
        let mirror = Mirror {
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
}
