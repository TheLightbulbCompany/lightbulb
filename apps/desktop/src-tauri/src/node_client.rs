//! Lightweight OpenClaw node client.
//!
//! Ported from OpenClaw's GatewayClient (src/gateway/client.ts) and
//! macOS companion app (GatewayNodeSession.swift). Implements the
//! OpenClaw WebSocket protocol for role:"node" connections.
//!
//! Handles: connect handshake, node.invoke.request dispatch, reconnection.

use base64::Engine as _;
use ed25519_dalek::pkcs8::DecodePrivateKey;
use ed25519_dalek::pkcs8::DecodePublicKey;
use ed25519_dalek::{Signer, SigningKey, VerifyingKey};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use tokio::sync::{mpsc, Mutex, oneshot};
use tokio_tungstenite::{connect_async, tungstenite::Message};
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
type InvokeSender = mpsc::UnboundedSender<NodeInvokeRequest>;
type StatusCallback = Arc<dyn Fn(&str) + Send + Sync>;

pub struct NodeClient {
    /// Shared so callers can update the URL (e.g. with a refreshed JWT) and
    /// the next reconnect picks it up. Guard is never held across await.
    url: Arc<RwLock<String>>,
    display_name: String,
    write_tx: Option<mpsc::UnboundedSender<Message>>,
    pending: PendingMap,
    stop_tx: Option<oneshot::Sender<()>>,
    status_cb: Option<StatusCallback>,
    /// Mac-slot Ed25519 keypair used to sign the upstream `connect.challenge`
    /// nonce. Task 14 removed backend signing — clients sign now.
    identity: DeviceIdentity,
    /// Gateway token from `/desktop/pair`. Bound into the v3 signature payload
    /// so the upstream container's signed-connect verifier can authenticate
    /// this device against the paired registry.
    gateway_token: GatewayTokenCell,
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
            pending: Arc::new(Mutex::new(HashMap::new())),
            stop_tx: None,
            status_cb: None,
            identity,
            gateway_token,
        }
    }

    /// Install a callback the connection loop invokes on status transitions.
    /// Possible status strings: "connecting", "connected", "error",
    /// "reconnecting", "disconnected". Call BEFORE start().
    pub fn on_status_change(&mut self, cb: impl Fn(&str) + Send + Sync + 'static) {
        self.status_cb = Some(Arc::new(cb));
    }

    /// Start the client. Returns a receiver for invoke requests.
    pub async fn start(&mut self) -> Result<mpsc::UnboundedReceiver<NodeInvokeRequest>, Box<dyn std::error::Error + Send + Sync>> {
        let (invoke_tx, invoke_rx) = mpsc::unbounded_channel::<NodeInvokeRequest>();
        let (stop_tx, stop_rx) = oneshot::channel::<()>();
        let (write_tx, write_rx) = mpsc::unbounded_channel::<Message>();

        self.write_tx = Some(write_tx.clone());
        self.stop_tx = Some(stop_tx);

        let url = self.url.clone();
        let display_name = self.display_name.clone();
        let pending = self.pending.clone();
        let status_cb = self.status_cb.clone();
        let identity = self.identity.clone();
        let gateway_token = self.gateway_token.clone();

        tokio::spawn(async move {
            connection_loop(
                url,
                display_name,
                write_rx,
                invoke_tx,
                pending,
                stop_rx,
                status_cb,
                identity,
                gateway_token,
            )
            .await;
        });

        Ok(invoke_rx)
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
            tx.send(Message::Text(frame.to_string().into()))
                .map_err(|e| format!("Send failed: {}", e))
        } else {
            Err("Not connected".into())
        }
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
    mut write_rx: mpsc::UnboundedReceiver<Message>,
    invoke_tx: InvokeSender,
    pending: PendingMap,
    mut stop_rx: oneshot::Receiver<()>,
    status_cb: Option<StatusCallback>,
    identity: DeviceIdentity,
    gateway_token: GatewayTokenCell,
) {
    let mut reconnect_delay = std::time::Duration::from_secs(1);
    let max_delay = std::time::Duration::from_secs(30);
    let emit_status = |s: &str| {
        if let Some(cb) = &status_cb {
            cb(s);
        }
    };

    // write_rx lives for the entire loop so that it stays paired with the
    // write_tx held by NodeClient. Prior design moved it into a per-connection
    // spawn and replaced it with a brand-new unpaired channel on reconnect —
    // after the first drop, every send_frame silently went nowhere. Keeping
    // ownership here and multiplexing via tokio::select! avoids that entirely.
    loop {
        // Snapshot the current URL so a mid-connect token update is picked up
        // on the very next reconnect attempt.
        let current_url = url.read().unwrap().clone();
        crate::log(&format!("[node-client] Connecting to {}...", mask_token(&current_url)));
        emit_status("connecting");

        let ws = tokio::select! {
            _ = &mut stop_rx => {
                crate::log("[node-client] Stop signal received during connect");
                emit_status("disconnected");
                return;
            }
            res = connect_async(&current_url) => match res {
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
        let drop_reason = loop {
            tokio::select! {
                // Clean shutdown.
                _ = &mut stop_rx => {
                    let _ = ws_write.close().await;
                    emit_status("disconnected");
                    return;
                }

                // Outbound frame from NodeClient → push to the socket.
                Some(msg) = write_rx.recv() => {
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
        emit_status("reconnecting");

        // Backoff before the next reconnect, respecting stop.
        tokio::select! {
            _ = &mut stop_rx => return,
            _ = tokio::time::sleep(reconnect_delay) => {}
        }
        reconnect_delay = std::cmp::min(reconnect_delay * 2, max_delay);
    }
}

/// Run the role:"node" connect handshake as a client-first/relay flow.
///
/// Post-Task-14 the backend is a pure relay: backend used to synthesize a
/// hello-ok itself from a stub EFS keypair, but that path was deleted when
/// per-user EFS device keys went away. The real handshake is now upstream
/// container -> backend -> Tauri-mac, with the Mac signing its own slot's
/// challenge from the Keychain Mac-slot key.
///
/// Wire flow:
///   1. We send the wake-up `connect` req (frame 1, no `device`). This kicks
///      backend's `handle_node_connect` into opening the upstream socket.
///   2. Backend forwards the upstream container's `connect.challenge` DOWN
///      to us via Management API.
///   3. We sign the challenge nonce (v3 payload, base64url Ed25519
///      signature, raw 32-byte pubkey base64url) and send a SIGNED `connect`
///      req (frame 2). Backend's $default handler routes it via
///      `submit_signed_connect` to the awaiting `NodeUpstreamConnection`.
///   4. Backend forwards the signed connect upstream; upstream returns
///      hello-ok; backend rewrites the res.id to match our wake-up req.id
///      and pushes it down.
///   5. We resolve on res(ok:true) for the wake-up id and continue into
///      steady-state node-invoke handling.
///
/// 30s overall handshake timeout (one budget covering the wake-up + the
/// challenge round-trip — neither leg has a deterministic latency floor
/// because both involve Management API pushes).
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

    // Step 1: wake-up `connect`. No device — backend uses this to spawn
    // handle_node_connect, then races the upstream challenge back down to us.
    let wakeup_id = Uuid::new_v4().to_string();
    let wakeup_msg = json!({
        "type": "req",
        "id": wakeup_id,
        "method": "connect",
        "params": {
            "minProtocol": 4,
            "maxProtocol": 4,
            "client": {
                "id": CLIENT_ID,
                "displayName": display_name,
                "version": "1.0.0",
                "platform": platform,
                "mode": CLIENT_MODE,
                "instanceId": Uuid::new_v4().to_string(),
            },
            "role": ROLE,
            "scopes": scopes,
            "caps": ["system"],
            // Mac-native commands only — mirror of DESKTOP_MAC_COMMANDS in
            // apps/backend/core/services/desktop_pair.py. system.run* and
            // file/dir/browser/exec-approvals belong to the openclaw CLI
            // node-host subprocess, not this Tauri-mac process.
            "commands": [
                "system.notify",
                "screen.snapshot",
                "device.info",
                "device.status",
                "device.health",
                "device.permissions",
            ],
            "pathEnv": std::env::var("PATH").unwrap_or_default(),
            "auth": {},
        },
    });
    ws_write
        .send(Message::Text(wakeup_msg.to_string().into()))
        .await
        .map_err(|e| format!("write wake-up connect: {}", e))?;

    // Steps 2-5: read frames until we see hello-ok for our wake-up id, or
    // we run out of budget. On the way we expect exactly one
    // `connect.challenge` event — when it lands, sign and send the signed
    // connect (frame 2). Any other intermediate frames (ticks, etc.) are
    // ignored.
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
            )?;
            signed_connect_id = Some(signed_id);
            ws_write
                .send(Message::Text(signed.to_string().into()))
                .await
                .map_err(|e| format!("write signed connect: {}", e))?;
            continue;
        }

        // res → either the wake-up's hello-ok (id rewritten by backend) or,
        // less likely, an error res for the signed connect.
        if frame.get("type").and_then(|v| v.as_str()) != Some("res") {
            continue;
        }
        let id = frame.get("id").and_then(|v| v.as_str()).unwrap_or("");
        let is_wakeup = id == wakeup_id;
        let is_signed = signed_connect_id.as_deref() == Some(id);
        if !is_wakeup && !is_signed {
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

/// Extract the 32 raw Ed25519 public-key bytes from a PEM string. Matches
/// upstream's `derivePublicKeyRaw` (src/infra/device-identity.ts), which
/// strips the 12-byte SPKI prefix `302a300506032b6570032100` from a DER-
/// encoded SPKI to recover the raw key. Going via `VerifyingKey::to_bytes`
/// is equivalent — ed25519-dalek owns that decoding path.
fn raw_public_key_from_pem(public_key_pem: &str) -> Result<[u8; 32], String> {
    let verifying = VerifyingKey::from_public_key_pem(public_key_pem)
        .map_err(|e| format!("parse public PEM: {e}"))?;
    Ok(verifying.to_bytes())
}

/// `sha256(rawPub).hex()` — same formula upstream uses to mint
/// `deviceId`. The signed connect's `device.id` MUST equal this so the
/// upstream container can look up the paired entry by deviceId.
fn derive_device_id(public_key_pem: &str) -> Result<String, String> {
    let raw = raw_public_key_from_pem(public_key_pem)?;
    let mut hasher = Sha256::new();
    hasher.update(raw);
    Ok(hex::encode(hasher.finalize()))
}

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

async fn handle_message(text: &str, invoke_tx: &InvokeSender, pending: &PendingMap) {
    let Ok(frame) = serde_json::from_str::<Value>(text) else {
        return;
    };

    let msg_type = frame.get("type").and_then(|v| v.as_str()).unwrap_or("");

    match msg_type {
        "event" => {
            // The connect.challenge handshake runs inline in connection_loop
            // (see perform_handshake). By the time we're dispatching events
            // here, we're post-handshake and should only see runtime events.
            let event = frame.get("event").and_then(|v| v.as_str()).unwrap_or("");
            if event == "node.invoke.request" {
                if let Some(payload) = frame.get("payload") {
                    if let Ok(req) = serde_json::from_value::<NodeInvokeRequest>(payload.clone()) {
                        let _ = invoke_tx.send(req);
                    }
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
    use ed25519_dalek::Verifier;

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
}
