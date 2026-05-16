//! Loopback WSS proxy.
//!
//! The bundled openclaw subprocess (see node_invoke / Task 19) connects
//! to `ws://127.0.0.1:<random>/` instead of dialing the gateway directly.
//! This proxy then opens the real upstream WSS, injecting the Clerk JWT
//! and clientId into the query string, and bridges frames in both
//! directions.
//!
//! Why a proxy:
//!
//! 1. The subprocess shouldn't know about Clerk auth — JWT rotation
//!    stays in the Tauri host (Task 21 plumbs a shared `RwLock<String>`
//!    into the [`JwtProvider`] callback).
//! 2. We need to perform the OpenClaw "client-first wake-up" handshake
//!    on every upstream connect — the same dance `node_client.rs` already
//!    does for the chat path — so that API Gateway's Management API
//!    doesn't return `GoneException` on the first server-initiated push.
//!    The subprocess upstream protocol layer would also send a connect
//!    frame, but doing it from the proxy guarantees it lands before any
//!    server-side bind logic runs, eliminating the race.

use futures_util::{SinkExt, StreamExt};
use std::sync::Arc;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::Message;

/// Callback the proxy invokes whenever it opens a new upstream connection
/// so it can fetch the current Clerk JWT. Task 21 wires this to the shared
/// `Arc<RwLock<String>>` that already backs the chat WS in `lib.rs`.
pub type JwtProvider = Arc<dyn Fn() -> String + Send + Sync>;

/// Handle returned by [`start_proxy`]. Owns the bound port and the
/// shutdown signal for the listener loop.
pub struct ProxyHandle {
    port: u16,
    shutdown_tx: Option<oneshot::Sender<()>>,
}

impl ProxyHandle {
    /// The 127.0.0.1 port the proxy is listening on. Hand this to the
    /// openclaw subprocess via `ISOL8_PROXY_PORT` (Task 20).
    pub fn local_port(&self) -> u16 {
        self.port
    }

    /// Signal the listener loop to exit. In-flight connections are
    /// dropped when the listener task is cancelled.
    pub async fn shutdown(mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(());
        }
    }
}

/// Bind `127.0.0.1:0`, return the bound port, and spawn the accept loop.
///
/// Every incoming subprocess connection opens a fresh upstream WSS so
/// that JWT rotation is picked up on the next reconnect — same pattern
/// as `node_client.rs`.
pub async fn start_proxy(
    upstream_url: String,
    client_id: String,
    jwt_provider: JwtProvider,
) -> Result<ProxyHandle, String> {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .map_err(|e| format!("proxy bind 127.0.0.1:0: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("proxy local_addr: {e}"))?
        .port();

    let (shutdown_tx, mut shutdown_rx) = oneshot::channel();

    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut shutdown_rx => {
                    break;
                }
                accepted = listener.accept() => {
                    let (stream, _peer) = match accepted {
                        Ok(v) => v,
                        Err(_) => continue,
                    };
                    let upstream_url = upstream_url.clone();
                    let client_id = client_id.clone();
                    let jwt_provider = jwt_provider.clone();
                    tokio::spawn(async move {
                        let _ = handle_connection(stream, upstream_url, client_id, jwt_provider).await;
                    });
                }
            }
        }
    });

    Ok(ProxyHandle {
        port,
        shutdown_tx: Some(shutdown_tx),
    })
}

/// Per-connection handler:
///
///  1. Accept the subprocess WS handshake on the loopback socket.
///  2. Fetch a fresh JWT via the provider callback.
///  3. Open the upstream WSS with `?token=…&clientId=…` injected.
///  4. Send the client-first `connect` wake-up frame upstream.
///  5. Spawn two halves of bidirectional frame forwarding.
async fn handle_connection(
    stream: TcpStream,
    upstream_url: String,
    client_id: String,
    jwt_provider: JwtProvider,
) -> Result<(), String> {
    let sub_ws = tokio_tungstenite::accept_async(stream)
        .await
        .map_err(|e| format!("sub-side accept: {e}"))?;
    let (mut sub_tx, mut sub_rx) = sub_ws.split();

    let jwt = jwt_provider();
    // urlencoding::encode is overkill here — Clerk JWTs are base64url and
    // clientId comes from us, but using `?` vs `&` correctly matters in
    // case the caller already embedded a query string in upstream_url.
    let separator = if upstream_url.contains('?') { '&' } else { '?' };
    let upstream_full = format!("{upstream_url}{separator}token={jwt}&clientId={client_id}");

    let upstream_ws = match tokio_tungstenite::connect_async(&upstream_full).await {
        Ok((ws, _resp)) => ws,
        Err(e) => return Err(format!("upstream connect: {e}")),
    };
    let (mut up_tx, mut up_rx) = upstream_ws.split();

    // Client-first wake-up. Mirror node_client.rs's connect shape: the
    // gateway routes the connection based on `params.role` + `params.client.id`,
    // and our minimum protocol version is 4 (PR #660 — protocol v4 floor).
    let wakeup = serde_json::json!({
        "type": "req",
        "id": uuid::Uuid::new_v4().to_string(),
        "method": "connect",
        "params": {
            "minProtocol": 4,
            "maxProtocol": 4,
            "role": "node",
            "client": {
                "id": client_id,
                "mode": "node",
            },
        },
    });
    if let Err(e) = up_tx.send(Message::Text(wakeup.to_string().into())).await {
        return Err(format!("send wake-up: {e}"));
    }

    // Bidirectional forwarding. Either side closing tears the bridge down.
    let sub_to_up = async {
        while let Some(msg) = sub_rx.next().await {
            let msg = match msg {
                Ok(m) => m,
                Err(_) => break,
            };
            if matches!(msg, Message::Close(_)) {
                let _ = up_tx.send(msg).await;
                break;
            }
            if up_tx.send(msg).await.is_err() {
                break;
            }
        }
    };
    let up_to_sub = async {
        while let Some(msg) = up_rx.next().await {
            let msg = match msg {
                Ok(m) => m,
                Err(_) => break,
            };
            if matches!(msg, Message::Close(_)) {
                let _ = sub_tx.send(msg).await;
                break;
            }
            if sub_tx.send(msg).await.is_err() {
                break;
            }
        }
    };

    tokio::select! {
        _ = sub_to_up => {}
        _ = up_to_sub => {}
    }
    Ok(())
}
