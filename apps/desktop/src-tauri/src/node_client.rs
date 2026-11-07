//! Lightweight OpenClaw node client.
//!
//! Ported from OpenClaw's GatewayClient (src/gateway/client.ts) and
//! macOS companion app (GatewayNodeSession.swift). Implements the
//! OpenClaw WebSocket protocol for role:"node" connections.
//!
//! Handles: connect handshake, node.invoke.request dispatch, reconnection.

use base64::Engine as _;
use ed25519_dalek::pkcs8::DecodePrivateKey;
use ed25519_dalek::{Signer, SigningKey};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use tokio::sync::{mpsc, Mutex, oneshot};
use tokio_tungstenite::tungstenite::Message;
use uuid::Uuid;

use crate::keychain::DeviceIdentity;

/// Read-only cell the host wires in after `/desktop/pair` returns. The node
/// client snapshots this on each connect so a mid-life token rotation flows
/// in on the next reconnect without restart.
pub type GatewayTokenCell = Arc<RwLock<Option<String>>>;

// --- Types (from OpenClaw protocol schema) ---

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NodeInvokeRequest {
    pub id: String,
    #[serde(rename = "nodeId")]
    pub node_id: String,
    pub command: String,
    #[serde(rename = "paramsJSON")]
    pub params_json: Option<String>,
    #[serde(rename = "timeoutMs")]
    pub timeout_ms: Option<u64>,
}

#[derive(Debug, Serialize)]
pub struct NodeInvokeResult {
    pub id: String,
    #[serde(rename = "nodeId")]
    pub node_id: String,
    pub ok: bool,
    #[serde(rename = "payloadJSON", skip_serializing_if = "Option::is_none")]
    pub payload_json: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<InvokeError>,
}

#[derive(Debug, Serialize)]
pub struct InvokeError {
    pub code: String,
    pub message: String,
}

// --- Node Client ---

type PendingMap = Arc<Mutex<HashMap<String, oneshot::Sender<Value>>>>;
pub enum NodeEvent {
    Invoke(NodeInvokeRequest),
    Cancel { invoke_id: String, node_id: String },
    Disconnected,
}
type InvokeSender = mpsc::Sender<NodeEvent>;
/// `browser.cdp.open|frame|close` and `browser.cast.set|input` from the backend:
/// `(event, payload)`. Its own lane, so a
/// CDP burst never waits behind (or delays) the 32-slot invoke channel.
pub type CdpInbound = (String, Value);
type CdpSender = mpsc::Sender<CdpInbound>;
const CDP_LANE: usize = 1024;
/// After this many CDP frames in a row, one waiting invoke/control frame goes first.
const CDP_STREAK: u32 = 64;
/// Inbound frame cap: a 16 MiB CDP `data` string plus its JSON envelope exceeds tungstenite's
/// 16 MiB default and would drop the whole node socket. The relay caps a frame at 25 MiB.
const MAX_INBOUND_FRAME: usize = 32 * 1024 * 1024;

/// The Mac-to-backend CDP lane. `send` awaits a free slot (backpressure), never drops.
#[derive(Clone)]
pub struct EventSender(mpsc::Sender<Message>);

/// `{"type":"event","event":<event>,"payload":<payload>}` as sent on the node socket.
pub fn event_text(event: &str, payload: Value) -> String {
    json!({"type": "event", "event": event, "payload": payload}).to_string()
}

impl EventSender {
    #[cfg(test)]
    pub(crate) fn new(tx: mpsc::Sender<Message>) -> Self {
        Self(tx)
    }

    pub async fn send(&self, event: &str, payload: Value) -> Result<(), String> {
        self.send_text(event_text(event, payload)).await
    }

    /// An already-encoded event (the caller measured its size).
    pub async fn send_text(&self, text: String) -> Result<(), String> {
        self.0.send(Message::Text(text.into())).await.map_err(|_| "node client stopped".to_string())
    }

    /// Nothing queued in the lane. Live view frames go only then, so they never pile up
    /// ahead of the agent's own CDP traffic on a slow uplink.
    pub fn is_idle(&self) -> bool {
        self.0.capacity() == self.0.max_capacity()
    }

    /// Enqueue without waiting: false when the lane is full (or closed) and the text dropped.
    pub fn try_send_text(&self, text: String) -> bool {
        self.0.try_send(Message::Text(text.into())).is_ok()
    }
}
type StatusCallback = Arc<dyn Fn(&str) + Send + Sync>;

/// Called on EVERY connect attempt to obtain the `$connect` token, mirroring
/// `proxy::JwtProvider` on the CLI node-host path (#1181).
///
/// Why this exists: the URL cell alone carried a ~60s Clerk JWT that ONLY the
/// webview refreshed (`send_auth_token`). When the webview does not run that
/// push — backgrounded launch, a lost desktop permission grant making
/// `useGateway` bail before it calls, an unloaded page — the node reconnects
/// forever with a dead token and 403s with no recovery. Measured on a live
/// install 2026-08-05: 66 consecutive `403 Forbidden` against 0 token pushes,
/// at normal scheduling priority (so not App Nap).
///
/// A provider inverts that: the node mints its own short-lived EdDSA device
/// token from its Keychain key each time, so it never depends on another
/// component having run.
pub type TokenProvider = Arc<dyn Fn() -> String + Send + Sync>;

/// Status the connection loop emits (then exits on) when the server no longer knows
/// this device. The host drops its pairing and re-pairs on the next webview token.
pub const STATUS_UNPAIRED: &str = "unpaired";

/// Detects a pairing the server removed (member removed, unpaired from another
/// device, workspace gone). The relay refuses an unknown device before accepting the
/// socket (`machine_auth.verify_upgrade` -> None -> close 1008), which reaches us as
/// HTTP 403 on the upgrade. A pre-accept 1011 (a DynamoDB blip) is also a 403, so one
/// refusal proves nothing: only an unbroken run of refusals, with self-minted tokens
/// (so never a stale webview JWT), for a while, counts. Any successful upgrade resets it.
#[derive(Default)]
pub(crate) struct StalePairing {
    first: Option<std::time::Instant>,
    count: u32,
}

impl StalePairing {
    const MIN_REFUSALS: u32 = 8;
    const MIN_SPAN: std::time::Duration = std::time::Duration::from_secs(5 * 60);

    /// Record one 403; true once the refusal is persistent.
    pub(crate) fn refused(&mut self, now: std::time::Instant) -> bool {
        let first = *self.first.get_or_insert(now);
        self.count += 1;
        self.count >= Self::MIN_REFUSALS && now.duration_since(first) >= Self::MIN_SPAN
    }

    pub(crate) fn reset(&mut self) {
        *self = Self::default();
    }
}

/// Replace (or insert) the `token` query parameter on a gateway URL.
///
/// Kept as a pure function so the rewrite is unit-testable without a socket;
/// the connect loop calls it per attempt with a freshly minted token.
fn with_fresh_token(base_url: &str, token: &str) -> String {
    match url::Url::parse(base_url) {
        Ok(mut u) => {
            let others: Vec<(String, String)> = u
                .query_pairs()
                .filter(|(k, _)| k != "token")
                .map(|(k, v)| (k.into_owned(), v.into_owned()))
                .collect();
            {
                let mut qp = u.query_pairs_mut();
                qp.clear();
                for (k, v) in &others {
                    qp.append_pair(k, v);
                }
                qp.append_pair("token", token);
            }
            u.to_string()
        }
        // Never drop the connection over a URL we cannot parse — fall back to
        // the caller's string and let the connect attempt report the real error.
        Err(_) => base_url.to_string(),
    }
}

pub struct NodeClient {
    /// Shared so callers can update the URL (e.g. with a refreshed JWT) and
    /// the next reconnect picks it up. Guard is never held across await.
    url: Arc<RwLock<String>>,
    display_name: String,
    write_tx: Option<mpsc::Sender<Message>>,
    cdp_out: Option<mpsc::Sender<Message>>,
    pending: PendingMap,
    stop_tx: Option<oneshot::Sender<()>>,
    status_cb: Option<StatusCallback>,
    /// Mac-slot Ed25519 keypair used to sign the upstream `connect.challenge`
    /// nonce. Clients sign the nonce; backend does not.
    identity: DeviceIdentity,
    /// Gateway token from `/desktop/pair`. Bound into the v3 signature payload
    /// so the upstream container's signed-connect verifier can authenticate
    /// this device against the paired registry.
    gateway_token: GatewayTokenCell,
    /// When set, mints the `$connect` token per attempt instead of trusting
    /// whatever token is baked into `url`. See [`TokenProvider`].
    token_provider: Option<TokenProvider>,
    /// Drops the live socket so the next connect picks up a changed server grant
    /// (the relay intersects commands once, at connect). See `request_reconnect`.
    reconnect: Arc<tokio::sync::Notify>,
}

impl NodeClient {
    pub fn new(
        url: &str,
        display_name: &str,
        identity: DeviceIdentity,
        gateway_token: GatewayTokenCell,
    ) -> Self {
        Self::with_shared_url(
            Arc::new(RwLock::new(url.to_string())),
            display_name,
            identity,
            gateway_token,
        )
    }

    /// Construct a client backed by a shared URL handle. Callers that need to
    /// rotate the token can keep a clone of the Arc and write to it; the
    /// connection loop re-reads before each reconnect.
    pub fn with_shared_url(
        url: Arc<RwLock<String>>,
        display_name: &str,
        identity: DeviceIdentity,
        gateway_token: GatewayTokenCell,
    ) -> Self {
        Self {
            url,
            display_name: display_name.to_string(),
            write_tx: None,
            cdp_out: None,
            pending: Arc::new(Mutex::new(HashMap::new())),
            stop_tx: None,
            status_cb: None,
            identity,
            gateway_token,
            token_provider: None,
            reconnect: Arc::new(tokio::sync::Notify::new()),
        }
    }

    /// Share the reconnect signal: `notify_one` on it drops the current socket and
    /// reconnects (a stored permit covers a signal sent while connecting).
    pub fn set_reconnect(&mut self, reconnect: Arc<tokio::sync::Notify>) {
        self.reconnect = reconnect;
    }

    /// Install the per-connect token provider. Call BEFORE start().
    ///
    /// With this set the node authenticates itself and no longer depends on
    /// the webview pushing a Clerk JWT — which is the failure that leaves it
    /// 403-looping with no recovery.
    pub fn set_token_provider(&mut self, provider: TokenProvider) {
        self.token_provider = Some(provider);
    }

    /// Install a callback the connection loop invokes on status transitions.
    /// Possible status strings: "connecting", "connected", "error",
    /// "reconnecting", "disconnected". Call BEFORE start().
    pub fn on_status_change(&mut self, cb: impl Fn(&str) + Send + Sync + 'static) {
        self.status_cb = Some(Arc::new(cb));
    }

    /// Start the client. Returns receivers for invoke requests and for CDP channel events.
    #[allow(clippy::type_complexity)]
    pub async fn start(
        &mut self,
    ) -> Result<(mpsc::Receiver<NodeEvent>, mpsc::Receiver<CdpInbound>), Box<dyn std::error::Error + Send + Sync>> {
        let (invoke_tx, invoke_rx) = mpsc::channel::<NodeEvent>(32);
        let (cdp_in_tx, cdp_in_rx) = mpsc::channel::<CdpInbound>(CDP_LANE);
        let (stop_tx, stop_rx) = oneshot::channel::<()>();
        let (write_tx, write_rx) = mpsc::channel::<Message>(16);
        let (cdp_out_tx, cdp_out_rx) = mpsc::channel::<Message>(CDP_LANE);

        self.write_tx = Some(write_tx.clone());
        self.cdp_out = Some(cdp_out_tx);
        self.stop_tx = Some(stop_tx);

        let url = self.url.clone();
        let display_name = self.display_name.clone();
        let pending = self.pending.clone();
        let status_cb = self.status_cb.clone();
        let identity = self.identity.clone();
        let gateway_token = self.gateway_token.clone();
        let token_provider = self.token_provider.clone();
        let reconnect = self.reconnect.clone();

        tokio::spawn(async move {
            connection_loop(
                url,
                display_name,
                write_rx,
                cdp_out_rx,
                invoke_tx,
                cdp_in_tx,
                pending,
                stop_rx,
                status_cb,
                identity,
                gateway_token,
                token_provider,
                reconnect,
            )
            .await;
        });

        Ok((invoke_rx, cdp_in_rx))
    }

    /// The CDP lane to the backend. Call after `start`.
    pub fn event_sender(&self) -> Option<EventSender> {
        self.cdp_out.clone().map(EventSender)
    }

    /// Send a node.invoke.result back to the gateway.
    pub async fn send_invoke_result(&self, result: NodeInvokeResult) -> Result<(), String> {
        let id = Uuid::new_v4().to_string();
        let frame = json!({
            "type": "req",
            "id": id,
            "method": "node.invoke.result",
            "params": result,
        });
        self.send_frame(frame).await
    }

    /// Send a JSON frame to the gateway.
    async fn send_frame(&self, frame: Value) -> Result<(), String> {
        if let Some(tx) = &self.write_tx {
            tx.try_send(Message::Text(frame.to_string().into()))
                .map_err(|e| format!("Send failed: {}", e))
        } else {
            Err("Not connected".into())
        }
    }

    /// Hand the stop signal to the host (call after `start`), so it can end this
    /// client after the client itself has moved into the invoke task.
    pub fn take_stop(&mut self) -> Option<oneshot::Sender<()>> {
        self.stop_tx.take()
    }

    pub async fn stop(&mut self) {
        if let Some(tx) = self.stop_tx.take() {
            let _ = tx.send(());
        }
        self.write_tx = None;
    }
}

// --- Connection loop with reconnection ---

async fn connection_loop(
    url: Arc<RwLock<String>>,
    display_name: String,
    mut write_rx: mpsc::Receiver<Message>,
    mut cdp_rx: mpsc::Receiver<Message>,
    invoke_tx: InvokeSender,
    cdp_in: CdpSender,
    pending: PendingMap,
    mut stop_rx: oneshot::Receiver<()>,
    status_cb: Option<StatusCallback>,
    identity: DeviceIdentity,
    gateway_token: GatewayTokenCell,
    token_provider: Option<TokenProvider>,
    reconnect: Arc<tokio::sync::Notify>,
) {
    let mut reconnect_delay = std::time::Duration::from_secs(1);
    let max_delay = std::time::Duration::from_secs(30);
    let mut stale = StalePairing::default();
    let emit_status = |s: &str| {
        if let Some(cb) = &status_cb {
            cb(s);
        }
    };

    // write_rx lives for the entire loop so that it stays paired with the
    // write_tx held by NodeClient. Multiplexing via tokio::select! keeps the
    // pairing intact across reconnects — moving write_rx into a per-connection
    // spawn would break send_frame the moment the first connection drops.
    loop {
        // Snapshot the current URL so a mid-connect token update is picked up
        // on the very next reconnect attempt.
        // Mint the $connect token fresh per attempt when a provider is set.
        // Without this the loop would retry a dead webview-supplied JWT
        // forever (66 straight 403s observed in prod, 0 refreshes).
        let base_url = url.read().unwrap().clone();
        let current_url = match &token_provider {
            Some(provider) => {
                let token = provider();
                if token.is_empty() {
                    crate::log("[node-client] token provider returned empty; connect will 403 and retry");
                }
                with_fresh_token(&base_url, &token)
            }
            None => base_url,
        };
        crate::log(&format!("[node-client] Connecting to {}...", mask_token(&current_url)));
        emit_status("connecting");

        let ws = tokio::select! {
            _ = &mut stop_rx => {
                crate::log("[node-client] Stop signal received during connect");
                emit_status("disconnected");
                return;
            }
            res = tokio_tungstenite::connect_async_with_config(
                &current_url,
                Some(tokio_tungstenite::tungstenite::protocol::WebSocketConfig::default()
                    .max_frame_size(Some(MAX_INBOUND_FRAME))
                    .max_message_size(Some(MAX_INBOUND_FRAME))),
                false,
            ) => match res {
                Ok((ws, _)) => ws,
                Err(e) => {
                    crate::log(&format!("[node-client] Connection failed: {}", e));
                    if let tokio_tungstenite::tungstenite::Error::Http(resp) = &e {
                        let status = resp.status();
                        let body = resp.body().as_ref()
                            .map(|b| String::from_utf8_lossy(b).to_string())
                            .unwrap_or_else(|| "<no body>".to_string());
                        let headers = resp.headers().iter()
                            .map(|(k, v)| format!("{}={}", k, v.to_str().unwrap_or("?")))
                            .collect::<Vec<_>>().join("; ");
                        crate::log(&format!("[node-client] HTTP {} body={} headers={}",
                            status, truncate(&body, 400), truncate(&headers, 400)));
                        if status == 403
                            && token_provider.is_some()
                            && stale.refused(std::time::Instant::now())
                        {
                            crate::log("[node-client] device refused persistently; the pairing is gone");
                            emit_status(STATUS_UNPAIRED);
                            return;
                        }
                    }
                    emit_status("error");
                    // Backoff, respecting stop.
                    tokio::select! {
                        _ = &mut stop_rx => return,
                        _ = tokio::time::sleep(reconnect_delay) => {}
                    }
                    reconnect_delay = std::cmp::min(reconnect_delay * 2, max_delay);
                    continue;
                }
            }
        };

        stale.reset();
        crate::log("[node-client] WebSocket open, starting handshake");

        let (mut ws_write, mut ws_read) = ws.split();

        // Perform the connect handshake inline. If it fails — socket error OR
        // a res with ok:false — close the socket and reconnect. Previously
        // handshake lived in handle_message, which treated every res as a
        // generic pending-response and left a rejected connect in a zombie
        // state (socket open, no invokes will ever arrive).
        // Snapshot the gateway token under the read guard, then drop it
        // before the await on perform_handshake.
        let token_snapshot = gateway_token
            .read()
            .ok()
            .and_then(|g| g.clone());
        match perform_handshake(
            &mut ws_read,
            &mut ws_write,
            &display_name,
            &identity,
            token_snapshot.as_deref(),
        )
        .await
        {
            Ok(()) => {
                crate::log("[node-client] Handshake complete");
                emit_status("connected");
                reconnect_delay = std::time::Duration::from_secs(1);
            }
            Err(reason) => {
                crate::log(&format!("[node-client] Handshake failed: {}", reason));
                emit_status("error");
                let _ = ws_write.close().await;
                tokio::select! {
                    _ = &mut stop_rx => return,
                    _ = tokio::time::sleep(reconnect_delay) => {}
                }
                reconnect_delay = std::cmp::min(reconnect_delay * 2, max_delay);
                continue;
            }
        }

        // Process messages until either the socket dies or we get a stop.
        // write_rx stays borrowed from the outer scope — no ownership move, no
        // pairing loss on reconnect.
        let mut streak = 0u32;
        let drop_reason = loop {
            tokio::select! {
                // Clean shutdown.
                _ = &mut stop_rx => {
                    let _ = invoke_tx.send(NodeEvent::Disconnected).await;
                    let _ = ws_write.close().await;
                    emit_status("disconnected");
                    return;
                }

                // The server grant changed (re-pair): reconnect so it is re-read.
                _ = reconnect.notified() => {
                    let _ = ws_write.close().await;
                    break "pairing grants changed".to_string();
                }

                // Outbound frame: the CDP lane first, the invoke/control lane never starved.
                Some(msg) = next_outbound(&mut cdp_rx, &mut write_rx, &mut streak) => {
                    if let Err(e) = ws_write.send(msg).await {
                        break format!("ws_write failed: {}", e);
                    }
                }

                // Inbound frame from the gateway → dispatch.
                next = ws_read.next() => {
                    match next {
                        Some(Ok(Message::Text(text))) => {
                            handle_message(
                                &text,
                                &invoke_tx,
                                &cdp_in,
                                &pending,
                            ).await;
                        }
                        Some(Ok(_)) => {
                            // Ignore non-text frames (ping/pong handled by the lib).
                        }
                        Some(Err(e)) => break format!("ws_read error: {}", e),
                        None => break "ws_read closed".to_string(),
                    }
                }
            }
        };

        crate::log(&format!("[node-client] Disconnected ({}); reconnecting", drop_reason));
        let _ = invoke_tx.send(NodeEvent::Disconnected).await;
        while write_rx.try_recv().is_ok() {} // never replay old results onto a new socket
        while cdp_rx.try_recv().is_ok() {} // nor CDP frames for channels that socket carried
        emit_status("reconnecting");

        // Backoff before the next reconnect, respecting stop.
        tokio::select! {
            _ = &mut stop_rx => return,
            _ = tokio::time::sleep(reconnect_delay) => {}
        }
        reconnect_delay = std::cmp::min(reconnect_delay * 2, max_delay);
    }
}

/// The next outbound frame: CDP first, but after `CDP_STREAK` CDP frames in a row one waiting
/// invoke/control frame goes through. Cancel-safe (each arm is an mpsc `recv`).
async fn next_outbound(
    cdp: &mut mpsc::Receiver<Message>,
    write: &mut mpsc::Receiver<Message>,
    streak: &mut u32,
) -> Option<Message> {
    if *streak >= CDP_STREAK {
        *streak = 0;
        if let Ok(msg) = write.try_recv() {
            return Some(msg);
        }
    }
    tokio::select! {
        biased;
        Some(msg) = cdp.recv() => {
            *streak += 1;
            Some(msg)
        }
        Some(msg) = write.recv() => {
            *streak = 0;
            Some(msg)
        }
        else => None,
    }
}

/// Run the role:"node" connect handshake as a SERVER-first/relay flow.
///
/// The backend is a pure relay and starts the upstream handshake ITSELF the
/// moment our WebSocket opens (`_kick_off_node_connect`, gated on
/// clientId ∈ {node-host, openclaw-macos}). So we send nothing until the
/// container's challenge lands.
///
/// Wire flow:
///   1. Backend forwards the upstream container's `connect.challenge` DOWN
///      to us via Management API.
///   2. We sign the challenge nonce (v3 payload, base64url Ed25519
///      signature, raw 32-byte pubkey base64url) and send a SIGNED `connect`
///      req. Backend's $default handler routes it via `submit_signed_connect`
///      to the awaiting `NodeUpstreamConnection`.
///   3. Backend forwards the signed connect upstream; upstream returns
///      hello-ok; backend rewrites the res.id to our signed req.id and
///      pushes it down.
///   4. We resolve on res(ok:true) and continue into steady-state
///      node-invoke handling.
///
/// **The signed connect must be the FIRST AND ONLY `connect` frame we send.**
/// The relay's `$default` handler forwards *any* node-role connect frame
/// upstream verbatim as if it were the signed reply
/// (`routers/websocket_chat.py`). This client used to open with a deviceless
/// "wake-up" connect — a relic of the pre-`$connect` trigger — which raced the
/// real signed frame, usually won, and made the container answer
/// `NOT_PAIRED / DEVICE_IDENTITY_REQUIRED`. Measured on the owner's prod
/// install 2026-08-31: 282 of 286 handshakes over 19 hours, i.e. local tools
/// never connect. Do not reintroduce a frame without `params.device`.
///
/// 30s overall handshake timeout (the challenge and the hello-ok both ride
/// Management API pushes, so neither leg has a deterministic latency floor).
async fn perform_handshake<R, W>(
    ws_read: &mut R,
    ws_write: &mut W,
    display_name: &str,
    identity: &DeviceIdentity,
    gateway_token: Option<&str>,
) -> Result<(), String>
where
    R: StreamExt<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin,
    W: SinkExt<Message> + Unpin,
    <W as futures_util::Sink<Message>>::Error: std::fmt::Display,
{
    const HANDSHAKE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);
    const CLIENT_ID: &str = "openclaw-macos";
    const ROLE: &str = "node";
    const CLIENT_MODE: &str = "node";
    let scopes: Vec<String> = Vec::new();
    let platform = std::env::consts::OS; // "macos"

    // Read frames until we see hello-ok for our signed connect, or we run
    // out of budget. On the way we expect exactly one `connect.challenge`
    // event — when it lands, sign and send the signed connect. Any other
    // intermediate frames (ticks, etc.) are ignored.
    let deadline = tokio::time::Instant::now() + HANDSHAKE_TIMEOUT;
    let mut signed_connect_id: Option<String> = None;
    loop {
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return Err("handshake timed out".to_string());
        }
        let remaining = deadline - now;
        let msg = tokio::time::timeout(remaining, ws_read.next())
            .await
            .map_err(|_| "handshake timed out".to_string())?
            .ok_or_else(|| "connection closed during handshake".to_string())?
            .map_err(|e| format!("read error: {}", e))?;
        let Message::Text(text) = msg else { continue };
        crate::log(&format!(
            "[node-client] handshake frame: {}",
            truncate(&text, 300)
        ));
        let frame: Value = serde_json::from_str(&text).map_err(|e| format!("parse: {}", e))?;

        // connect.challenge → sign + send signed connect.
        if frame.get("type").and_then(|v| v.as_str()) == Some("event")
            && frame.get("event").and_then(|v| v.as_str()) == Some("connect.challenge")
        {
            let nonce = frame
                .pointer("/payload/nonce")
                .and_then(|n| n.as_str())
                .ok_or_else(|| "connect.challenge missing payload.nonce".to_string())?;
            let signed_id = Uuid::new_v4().to_string();
            let signed = build_signed_connect(
                identity,
                nonce,
                gateway_token,
                CLIENT_ID,
                display_name,
                ROLE,
                CLIENT_MODE,
                platform,
                &scopes,
                &signed_id,
            )
            // Loud on purpose. A connect frame with no `device` is
            // indistinguishable from a legacy wake-up on the wire and gets
            // rejected as NOT_PAIRED with no hint of why — so if we cannot
            // build a signed one we say so and let the socket fail, rather
            // than sending an unsigned frame that "almost" works.
            .map_err(|e| format!("cannot sign device identity ({e}) — not sending an unsigned connect"))?;
            signed_connect_id = Some(signed_id);
            ws_write
                .send(Message::Text(signed.to_string().into()))
                .await
                .map_err(|e| format!("write signed connect: {}", e))?;
            continue;
        }

        // res → the hello-ok (or a rejection) for our signed connect. The
        // backend rewrites the container's res.id to our signed req.id
        // (`NodeUpstreamConnection.signed_req_id`), so this is the only id
        // we can match on.
        if frame.get("type").and_then(|v| v.as_str()) != Some("res") {
            continue;
        }
        let id = frame.get("id").and_then(|v| v.as_str()).unwrap_or("");
        if signed_connect_id.as_deref() != Some(id) {
            // res for some other req we didn't send — ignore.
            continue;
        }
        if frame.get("ok").and_then(|v| v.as_bool()) != Some(true) {
            let err = frame
                .get("error")
                .and_then(|v| v.get("message"))
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            return Err(format!("gateway rejected connect: {}", err));
        }
        return Ok(());
    }
}

// ----------------------------------------------------------------------
// Device-auth signing helpers — mirror upstream openclaw
// (src/gateway/device-auth.ts::buildDeviceAuthPayloadV3 and
// src/infra/device-identity.ts::signDevicePayload). Any drift here will
// silently fail upstream's signature verifier — keep them locked to upstream.
// ----------------------------------------------------------------------

// `derive_device_id` and `raw_public_key_from_pem` live in core's `device_token`;
// `pair_state` and the session enrollment derive the same id.
pub(crate) use lightbulb_core::device_token::derive_device_id;
use lightbulb_core::device_token::raw_public_key_from_pem;

/// base64url-no-pad of the raw 32-byte public key — what upstream sends as
/// `device.publicKey` on the wire (`publicKeyRawBase64UrlFromPem`).
fn public_key_raw_base64url(public_key_pem: &str) -> Result<String, String> {
    let raw = raw_public_key_from_pem(public_key_pem)?;
    Ok(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(raw))
}

/// Sign the v3 device-auth payload string with the Mac slot's Ed25519
/// private key (PEM, PKCS8). Returns base64url-no-pad of the 64-byte
/// signature — matches upstream `signDevicePayload`.
fn sign_v3_payload(private_key_pem: &str, payload: &str) -> Result<String, String> {
    let signing = SigningKey::from_pkcs8_pem(private_key_pem)
        .map_err(|e| format!("parse private PEM: {e}"))?;
    let sig = signing.sign(payload.as_bytes());
    Ok(base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(sig.to_bytes()))
}

/// Build the v3 canonical payload string. EXACT order per upstream
/// `buildDeviceAuthPayloadV3` (src/gateway/device-auth.ts):
///   v3|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce|platform|deviceFamily
/// scopes is comma-joined. token, platform, deviceFamily are normalized
/// (trimmed + lowercased-ASCII) to "" when null/empty. Get this wrong and
/// the upstream container's signature verifier returns null and the
/// handshake fails.
#[allow(clippy::too_many_arguments)]
fn build_v3_payload_string(
    device_id: &str,
    client_id: &str,
    client_mode: &str,
    role: &str,
    scopes: &[String],
    signed_at_ms: i64,
    token: Option<&str>,
    nonce: &str,
    platform: &str,
    device_family: Option<&str>,
) -> String {
    let scopes_joined = scopes.join(",");
    let token_norm = token.unwrap_or("");
    let platform_norm = normalize_metadata(platform);
    let device_family_norm = normalize_metadata(device_family.unwrap_or(""));
    [
        "v3",
        device_id,
        client_id,
        client_mode,
        role,
        &scopes_joined,
        &signed_at_ms.to_string(),
        token_norm,
        nonce,
        &platform_norm,
        &device_family_norm,
    ]
    .join("|")
}

/// Mirror of `normalizeDeviceMetadataForAuth` (upstream
/// device-metadata-normalization.ts): trim, then lowercase ASCII only
/// (deliberately deterministic across runtimes — we must NOT lowercase
/// non-ASCII codepoints).
fn normalize_metadata(value: &str) -> String {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    trimmed
        .chars()
        .map(|c| {
            if c.is_ascii_uppercase() {
                ((c as u8) + 32) as char
            } else {
                c
            }
        })
        .collect()
}

/// Build the SIGNED connect req frame. Mirrors upstream client.ts:547-591.
#[allow(clippy::too_many_arguments)]
fn build_signed_connect(
    identity: &DeviceIdentity,
    nonce: &str,
    gateway_token: Option<&str>,
    client_id: &str,
    display_name: &str,
    role: &str,
    client_mode: &str,
    platform: &str,
    scopes: &[String],
    req_id: &str,
) -> Result<Value, String> {
    let device_id = derive_device_id(&identity.public_key_pem)?;
    let public_key_b64url = public_key_raw_base64url(&identity.public_key_pem)?;
    let signed_at_ms: i64 = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    let payload = build_v3_payload_string(
        &device_id,
        client_id,
        client_mode,
        role,
        scopes,
        signed_at_ms,
        gateway_token,
        nonce,
        platform,
        None,
    );
    let signature = sign_v3_payload(&identity.private_key_pem, &payload)?;

    Ok(json!({
        "type": "req",
        "id": req_id,
        "method": "connect",
        "params": {
            "minProtocol": 4,
            "maxProtocol": 4,
            "client": {
                "id": client_id,
                "displayName": display_name,
                "version": "1.0.0",
                "platform": platform,
                "mode": client_mode,
                "instanceId": Uuid::new_v4().to_string(),
            },
            "role": role,
            "scopes": scopes,
            "caps": ["system"],
            "commands": [
                "system.run.approve.v1",
                "desktop.operation.approve.v1",
                "file.access.policy.v1",
                "session.options.v1",
                "session.start.v1",
                "browser.cdp.v1",
                "browser.cast.v1",
                "browser.wipe.v1",
                "system.notify",
                "screen.snapshot",
                "device.info",
                "device.status",
                "device.health",
                "device.permissions",
            ],
            "pathEnv": std::env::var("PATH").unwrap_or_default(),
            "auth": gateway_token.map(|t| json!({ "token": t })).unwrap_or_else(|| json!({})),
            "device": {
                "id": device_id,
                "publicKey": public_key_b64url,
                "signature": signature,
                "signedAt": signed_at_ms,
                "nonce": nonce,
            },
        },
    }))
}

async fn handle_message(text: &str, invoke_tx: &InvokeSender, cdp_in: &CdpSender, pending: &PendingMap) {
    let Ok(mut frame) = serde_json::from_str::<Value>(text) else {
        return;
    };

    let msg_type = frame.get("type").and_then(|v| v.as_str()).unwrap_or("");

    match msg_type {
        "event" => {
            // The connect.challenge handshake runs inline in connection_loop
            // (see perform_handshake). By the time we're dispatching events
            // here, we're post-handshake and should only see runtime events.
            let event = frame.get("event").and_then(|v| v.as_str()).unwrap_or("").to_string();
            let event = event.as_str();
            if matches!(
                event,
                "browser.cdp.open" | "browser.cdp.frame" | "browser.cdp.close" | "browser.cdp.leased"
                    | "browser.cast.set" | "browser.cast.input"
            ) {
                if let Some(payload) = frame.get_mut("payload").map(Value::take) {
                    let _ = cdp_in.send((event.to_string(), payload)).await;
                }
                return;
            }
            if event == "node.invoke.request" {
                if let Some(payload) = frame.get("payload") {
                    if let Ok(req) = serde_json::from_value::<NodeInvokeRequest>(payload.clone()) {
                        if req.params_json.as_ref().map_or(0, String::len) <= 16_384 {
                            let _ = invoke_tx.send(NodeEvent::Invoke(req)).await;
                        }
                    }
                }
            }
            if event == "node.invoke.cancel" {
                if let (Some(id), Some(node)) = (frame.pointer("/payload/invokeId").and_then(Value::as_str),
                                                frame.pointer("/payload/nodeId").and_then(Value::as_str)) {
                    let _ = invoke_tx.send(NodeEvent::Cancel { invoke_id: id.into(), node_id: node.into() }).await;
                }
            }
            // tick events — ignore (keepalive)
        }
        "res" => {
            // Resolve any pending request waiting on this id.
            let id = frame.get("id").and_then(|v| v.as_str()).unwrap_or("");
            let mut pending = pending.lock().await;
            if let Some(sender) = pending.remove(id) {
                let _ = sender.send(frame);
            }
        }
        _ => {}
    }
}

fn truncate(s: &str, max: usize) -> String {
    if s.len() <= max {
        s.to_string()
    } else {
        // `&s[..max]` panics when `max` lands mid-codepoint on a multibyte
        // UTF-8 char. Walk back to the nearest char boundary so non-ASCII
        // payloads near the 300/400-byte trim limits can't crash the node
        // client mid-handshake.
        let mut boundary = max;
        while boundary > 0 && !s.is_char_boundary(boundary) {
            boundary -= 1;
        }
        format!("{}…(+{}b)", &s[..boundary], s.len() - boundary)
    }
}

fn mask_token(url: &str) -> String {
    if let Some(i) = url.find("token=") {
        let head = &url[..i + 6];
        let tail_start = i + 6;
        let tail = &url[tail_start..];
        let shown = tail.chars().take(8).collect::<String>();
        format!("{}{}…", head, shown)
    } else {
        url.to_string()
    }
}

#[cfg(test)]
mod handshake_tests {
    use super::*;
    use ed25519_dalek::pkcs8::DecodePublicKey;
    use ed25519_dalek::{Verifier, VerifyingKey};
    use sha2::{Digest, Sha256};

    /// V3 payload format MUST be exactly:
    ///   v3|deviceId|clientId|clientMode|role|scopes|signedAtMs|token|nonce|platform|deviceFamily
    /// (per upstream src/gateway/device-auth.ts::buildDeviceAuthPayloadV3).
    #[test]
    fn v3_payload_matches_upstream_format() {
        let p = build_v3_payload_string(
            "did-abc",
            "openclaw-macos",
            "node",
            "node",
            &[],
            1700000000000,
            Some("gw-tok"),
            "nonce-xyz",
            "macos",
            None,
        );
        assert_eq!(p, "v3|did-abc|openclaw-macos|node|node||1700000000000|gw-tok|nonce-xyz|macos|");
    }

    #[test]
    fn v3_payload_null_token_becomes_empty_string() {
        let p = build_v3_payload_string(
            "did",
            "openclaw-macos",
            "node",
            "node",
            &[],
            1,
            None,
            "n",
            "macos",
            None,
        );
        // The "token" slot must be empty (||), not "null".
        assert!(p.contains("||1||n|"));
    }

    #[test]
    fn metadata_normalization_trims_and_ascii_lowercases() {
        assert_eq!(normalize_metadata(" Macos "), "macos");
        assert_eq!(normalize_metadata(""), "");
        // Non-ASCII is preserved verbatim (matches upstream's toLowerAscii).
        assert_eq!(normalize_metadata("Á"), "Á");
    }

    #[test]
    fn signed_connect_signature_verifies_against_payload() {
        let identity = crate::keychain::generate_keypair();
        let frame = build_signed_connect(
            &identity,
            "nonce-abc",
            Some("gw-token"),
            "openclaw-macos",
            "Test Mac",
            "node",
            "node",
            "macos",
            &[],
            "req-id-1",
        )
        .unwrap();

        let device = frame.pointer("/params/device").unwrap();
        let device_id = device.get("id").unwrap().as_str().unwrap();
        let signature_b64 = device.get("signature").unwrap().as_str().unwrap();
        let signed_at_ms = device.get("signedAt").unwrap().as_i64().unwrap();
        let nonce = device.get("nonce").unwrap().as_str().unwrap();

        // Reconstruct the exact payload string that build_signed_connect
        // signed, then verify against the public key.
        let payload = build_v3_payload_string(
            device_id,
            "openclaw-macos",
            "node",
            "node",
            &[],
            signed_at_ms,
            Some("gw-token"),
            nonce,
            "macos",
            None,
        );
        let sig_bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD
            .decode(signature_b64)
            .unwrap();
        let sig = ed25519_dalek::Signature::from_bytes(sig_bytes.as_slice().try_into().unwrap());
        let vk = VerifyingKey::from_public_key_pem(&identity.public_key_pem).unwrap();
        vk.verify(payload.as_bytes(), &sig).unwrap();
    }

    #[test]
    fn device_id_is_sha256_hex_of_raw_pubkey() {
        let id = crate::keychain::generate_keypair();
        let device_id = derive_device_id(&id.public_key_pem).unwrap();
        let raw = raw_public_key_from_pem(&id.public_key_pem).unwrap();
        let mut h = Sha256::new();
        h.update(raw);
        let expected = hex::encode(h.finalize());
        assert_eq!(device_id, expected);
        // 64 hex chars (sha256 → 32 bytes).
        assert_eq!(device_id.len(), 64);
    }

    #[test]
    fn signed_frame_public_key_is_raw_base64url_no_pad() {
        let id = crate::keychain::generate_keypair();
        let frame = build_signed_connect(
            &id, "n", None, "openclaw-macos", "x", "node", "node", "macos", &[], "r1",
        )
        .unwrap();
        let pk = frame
            .pointer("/params/device/publicKey")
            .unwrap()
            .as_str()
            .unwrap();
        // 32-byte raw key → 43 base64-url chars (no padding).
        assert_eq!(pk.len(), 43);
        assert!(!pk.contains('='));
        assert!(!pk.contains('+'));
        assert!(!pk.contains('/'));
    }

    /// The handshake must put EXACTLY ONE `connect` frame on the wire, and it
    /// must carry `params.device`.
    ///
    /// The relay's `$default` handler forwards any node-role connect frame
    /// upstream verbatim as the signed reply
    /// (apps/backend/routers/websocket_chat.py), so a second, deviceless
    /// "wake-up" frame races the signed one, wins, and the container answers
    /// `NOT_PAIRED / DEVICE_IDENTITY_REQUIRED` — 282 of 286 handshakes on the
    /// owner's prod install 2026-08-31, i.e. local tools never connect.
    #[tokio::test]
    async fn handshake_sends_only_the_signed_connect() {
        let identity = crate::keychain::generate_keypair();
        let challenge = json!({
            "type": "event",
            "event": "connect.challenge",
            "payload": {"nonce": "nonce-1", "ts": 1_788_145_946_538i64},
        })
        .to_string();

        // Challenge, then EOF. We only care about what got written.
        let mut reads = futures_util::stream::iter(vec![Ok::<_, tokio_tungstenite::tungstenite::Error>(
            Message::Text(challenge.into()),
        )]);
        let mut writes: Vec<Message> = Vec::new();

        let res = perform_handshake(
            &mut reads,
            &mut writes,
            "Test Mac",
            &identity,
            Some("gw-token"),
        )
        .await;
        assert!(res.is_err(), "stream ends before hello-ok, so this must fail");

        assert_eq!(writes.len(), 1, "exactly one frame: the signed connect");
        let Message::Text(sent) = &writes[0] else {
            panic!("expected a text frame");
        };
        let frame: Value = serde_json::from_str(sent).unwrap();
        assert_eq!(frame.pointer("/method").unwrap(), "connect");
        assert_eq!(frame.pointer("/params/role").unwrap(), "node");
        let device_id = frame
            .pointer("/params/device/id")
            .and_then(|v| v.as_str())
            .expect("connect frame MUST carry params.device.id");
        assert_eq!(device_id, derive_device_id(&identity.public_key_pem).unwrap());
    }

    #[test]
    fn a_pairing_is_stale_only_after_persistent_refusals() {
        use std::time::{Duration, Instant};
        let t0 = Instant::now();
        let mut stale = StalePairing::default();
        // Many quick refusals (a DynamoDB blip) are not enough on their own.
        for i in 0..20 {
            assert!(!stale.refused(t0 + Duration::from_secs(i)), "refusal {i} within a minute");
        }
        // Long but sparse is not enough either.
        let mut sparse = StalePairing::default();
        assert!(!sparse.refused(t0));
        assert!(!sparse.refused(t0 + Duration::from_secs(600)));
        // An unbroken run past both bars is.
        assert!(stale.refused(t0 + Duration::from_secs(300)));
        // One successful upgrade starts the count over.
        stale.reset();
        assert!(!stale.refused(t0 + Duration::from_secs(900)));
    }

    // --- with_fresh_token ---
    //
    // Regression guard for the Mac node 403 loop (2026-08-05). The node
    // authenticated with a Clerk JWT that only the webview refreshed; when
    // that push never happened it retried a dead token forever — 66 straight
    // `403 Forbidden` against 0 refreshes, at normal scheduling priority.
    // These lock the URL rewrite that lets the node supply its own token.

    #[test]
    fn replaces_an_existing_stale_token() {
        let out = with_fresh_token("wss://ws.isol8.co/?token=STALE", "FRESH");
        assert!(out.contains("token=FRESH"), "{out}");
        assert!(!out.contains("STALE"), "stale token must not survive: {out}");
        assert_eq!(out.matches("token=").count(), 1, "exactly one token param: {out}");
    }

    #[test]
    fn inserts_a_token_when_absent() {
        let out = with_fresh_token("wss://ws.isol8.co/", "FRESH");
        assert!(out.contains("token=FRESH"), "{out}");
    }

    #[test]
    fn preserves_other_query_params() {
        let out = with_fresh_token("wss://ws.isol8.co/?clientId=openclaw-macos&token=STALE", "FRESH");
        assert!(out.contains("clientId=openclaw-macos"), "{out}");
        assert!(out.contains("token=FRESH"), "{out}");
        assert!(!out.contains("STALE"), "{out}");
    }

    #[test]
    fn signed_connect_declares_cdp_and_wipe() {
        let id = crate::keychain::generate_keypair();
        let frame = build_signed_connect(&id, "n", None, "openclaw-macos", "x", "node", "node", "macos", &[], "r1").unwrap();
        let commands = frame.pointer("/params/commands").unwrap().as_array().unwrap();
        assert!(commands.contains(&json!("browser.cdp.v1")));
        assert!(commands.contains(&json!("browser.cast.v1")));
        assert!(commands.contains(&json!("browser.wipe.v1")));
    }

    #[tokio::test]
    async fn handle_message_routes_cdp_events_to_their_own_lane() {
        let (invoke_tx, mut invoke_rx) = mpsc::channel(32);
        let (cdp_tx, mut cdp_rx) = mpsc::channel(8);
        let pending: PendingMap = Arc::new(Mutex::new(HashMap::new()));
        for event in ["browser.cdp.open", "browser.cdp.frame", "browser.cdp.close", "browser.cdp.leased", "browser.cast.set", "browser.cast.input"] {
            let text = json!({"type": "event", "event": event, "payload": {"channel": "c1", "data": "{}"}}).to_string();
            handle_message(&text, &invoke_tx, &cdp_tx, &pending).await;
            let (got, payload) = cdp_rx.try_recv().unwrap();
            assert_eq!(got, event);
            assert_eq!(payload["channel"], "c1");
        }
        assert!(invoke_rx.try_recv().is_err(), "CDP never rides the invoke channel");
        let invoke = json!({"type": "event", "event": "node.invoke.request",
            "payload": {"id": "i1", "nodeId": "n", "command": "device.health"}}).to_string();
        handle_message(&invoke, &invoke_tx, &cdp_tx, &pending).await;
        assert!(matches!(invoke_rx.try_recv(), Ok(NodeEvent::Invoke(_))));
        assert!(cdp_rx.try_recv().is_err());
    }

    fn text(m: Message) -> String {
        match m {
            Message::Text(t) => t.to_string(),
            _ => panic!("text frame"),
        }
    }

    #[tokio::test]
    async fn event_sender_reports_idle_and_try_send_never_waits() {
        let (tx, mut rx) = mpsc::channel(1);
        let out = EventSender::new(tx);
        assert!(out.is_idle());
        assert!(out.try_send_text("a".into()));
        assert!(!out.is_idle());
        assert!(!out.try_send_text("b".into()), "full: dropped, not awaited");
        rx.recv().await;
        assert!(out.is_idle());
    }

    #[tokio::test]
    async fn cdp_out_drains_before_invoke_results() {
        let (cdp_tx, mut cdp_rx) = mpsc::channel(8);
        let (write_tx, mut write_rx) = mpsc::channel(8);
        write_tx.send(Message::Text("invoke".into())).await.unwrap();
        for i in 0..3 {
            cdp_tx.send(Message::Text(format!("cdp{i}").into())).await.unwrap();
        }
        let mut streak = 0;
        let mut order = Vec::new();
        for _ in 0..4 {
            order.push(text(next_outbound(&mut cdp_rx, &mut write_rx, &mut streak).await.unwrap()));
        }
        assert_eq!(order, ["cdp0", "cdp1", "cdp2", "invoke"]);
    }

    #[tokio::test]
    async fn invoke_result_not_starved_by_cdp_burst() {
        let (cdp_tx, mut cdp_rx) = mpsc::channel(256);
        let (write_tx, mut write_rx) = mpsc::channel(8);
        for i in 0..200 {
            cdp_tx.send(Message::Text(format!("cdp{i}").into())).await.unwrap();
        }
        write_tx.send(Message::Text("invoke".into())).await.unwrap();
        let mut streak = 0;
        let mut order = Vec::new();
        for _ in 0..70 {
            order.push(text(next_outbound(&mut cdp_rx, &mut write_rx, &mut streak).await.unwrap()));
        }
        assert_eq!(order.iter().position(|m| m == "invoke"), Some(CDP_STREAK as usize));
        assert!(order[CDP_STREAK as usize + 1..].iter().all(|m| m.starts_with("cdp")));
    }

    #[test]
    fn unparseable_url_falls_back_rather_than_dropping_the_connection() {
        // A URL we cannot parse must not become an empty/garbage target; let
        // the connect attempt surface the real error instead.
        assert_eq!(with_fresh_token("not a url", "FRESH"), "not a url");
    }
}
