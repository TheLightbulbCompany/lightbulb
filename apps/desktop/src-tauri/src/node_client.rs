//! Lightweight OpenClaw node client.
//!
//! Ported from OpenClaw's GatewayClient (src/gateway/client.ts) and
//! macOS companion app (GatewayNodeSession.swift). Implements the
//! OpenClaw WebSocket protocol for role:"node" connections.
//!
//! Handles: connect handshake, node.invoke.request dispatch, reconnection.

use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use tokio::sync::{mpsc, Mutex, oneshot};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use uuid::Uuid;

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
}

impl NodeClient {
    pub fn new(url: &str, display_name: &str) -> Self {
        Self::with_shared_url(
            Arc::new(RwLock::new(url.to_string())),
            display_name,
        )
    }

    /// Construct a client backed by a shared URL handle. Callers that need to
    /// rotate the token can keep a clone of the Arc and write to it; the
    /// connection loop re-reads before each reconnect.
    pub fn with_shared_url(url: Arc<RwLock<String>>, display_name: &str) -> Self {
        Self {
            url,
            display_name: display_name.to_string(),
            write_tx: None,
            pending: Arc::new(Mutex::new(HashMap::new())),
            stop_tx: None,
            status_cb: None,
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

        tokio::spawn(async move {
            connection_loop(url, display_name, write_rx, invoke_tx, pending, stop_rx, status_cb).await;
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
        match perform_handshake(&mut ws_read, &mut ws_write, &display_name).await {
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

/// Run the OpenClaw role:"node" connect handshake. Fails fast on:
/// - timeout (10s per step)
/// - any I/O error reading the challenge or res
/// - a `res` for our connect req with `ok: false`
async fn perform_handshake<R, W>(
    ws_read: &mut R,
    ws_write: &mut W,
    display_name: &str,
) -> Result<(), String>
where
    R: StreamExt<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin,
    W: SinkExt<Message> + Unpin,
    <W as futures_util::Sink<Message>>::Error: std::fmt::Display,
{
    const STEP_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

    // Cloud-native handshake (client speaks first).
    //
    // In native OpenClaw deployments, the gateway sends `connect.challenge`
    // inline on WS open and the client signs the nonce. Our cloud bridge
    // (AWS API Gateway → FastAPI backend) can only push frames back to a
    // client through `boto3.post_to_connection` (Management API), which
    // races against API Gateway finalizing the WebSocket after $connect —
    // the push frequently lands in API Gateway's transient "GoneException"
    // window (~37ms post-$connect on dev) and the frame is silently
    // swallowed by management_api_client.py's GoneException handler, so a
    // server-first handshake stalls until our 10s timeout. The backend
    // already supports a client-first flow for operator clients (chat WS)
    // and signs the upstream container's real challenge itself with the
    // per-user EFS device key in `handle_node_connect`, so any nonce
    // signing we'd do here is decorative — the backend ignores it.
    //
    // Send `connect` immediately. Backend's `/ws/message` handler
    // (apps/backend/routers/websocket_chat.py:343) processes it
    // synchronously, opens the upstream WS to the container, completes
    // the real handshake server-side, and replies `hello-ok` on the same
    // response cycle. No Management API push, no race.

    // Step 1: send connect.
    let connect_id = Uuid::new_v4().to_string();
    let connect_msg = json!({
        "type": "req",
        "id": connect_id,
        "method": "connect",
        "params": {
            "minProtocol": 3,
            "maxProtocol": 3,
            "client": {
                "id": "node-host",
                "displayName": display_name,
                "version": "1.0.0",
                "platform": std::env::consts::OS,
                "mode": "node",
                "instanceId": Uuid::new_v4().to_string(),
            },
            "role": "node",
            "scopes": [],
            "caps": ["system"],
            // Commands we IMPLEMENT. Advertising only the ones we handle
            // avoids surfacing false capabilities to OpenClaw's node catalog.
            // Heavy capabilities (camera/photos/screen/location/notifications)
            // are still in node_invoke's dispatch as NOT_IMPLEMENTED stubs
            // so invokes surface a clean error instead of timing out, but
            // we don't claim to support them here.
            "commands": [
                "system.run.prepare",
                "system.run",
                "system.which",
                "system.execApprovals.get",
                "system.execApprovals.set",
                "system.notify",
                "device.info",
                "device.status",
                "device.health",
                "device.permissions",
                "browser.proxy",
                // file-transfer + screen capture commands (handlers in
                // src/file_transfer.rs). Path-policy enforcement happens
                // client-side via file_access::ensure_inside_allowlist;
                // gateway's allowedPaths is defense-in-depth only.
                "file.fetch",
                "file.write",
                "dir.list",
                "dir.fetch",
                "screen.snapshot",
            ],
            "pathEnv": std::env::var("PATH").unwrap_or_default(),
            "auth": {},
        },
    });
    ws_write
        .send(Message::Text(connect_msg.to_string().into()))
        .await
        .map_err(|e| format!("write connect: {}", e))?;

    // Step 2: wait for the res with our connect_id, skipping unrelated
    // frames. The backend (apps/backend/routers/websocket_chat.py) rewrites
    // the forwarded hello's id to match our req_id before sending, so strict
    // id-match is correct per JSON-RPC correlation. Fail on ok:false so a
    // rejected handshake forces a reconnect instead of leaving a zombie
    // open socket.
    loop {
        let msg = tokio::time::timeout(STEP_TIMEOUT, ws_read.next())
            .await
            .map_err(|_| "timed out waiting for connect res".to_string())?
            .ok_or_else(|| "connection closed before connect res".to_string())?
            .map_err(|e| format!("read error: {}", e))?;
        let Message::Text(text) = msg else { continue };
        crate::log(&format!("[node-client] post-connect frame: {}", truncate(&text, 300)));
        let frame: Value = serde_json::from_str(&text).map_err(|e| format!("parse: {}", e))?;
        if frame.get("type").and_then(|v| v.as_str()) != Some("res") {
            continue;
        }
        if frame.get("id").and_then(|v| v.as_str()) != Some(connect_id.as_str()) {
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
