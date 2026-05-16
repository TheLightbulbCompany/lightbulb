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
//! 2. JWT rotation: the proxy reads the current token via [`JwtProvider`]
//!    on every upstream-open so reconnects pick up fresh credentials
//!    without bouncing the subprocess.
//!
//! Frame handling is a transparent bidirectional relay — the proxy does
//! NOT inject any synthetic frames. The OpenClaw v3 protocol is
//! server-initiated (server emits `connect.challenge`, client signs and
//! responds with a single `connect` request), so a proxy-injected
//! `connect` frame upstream would be misidentified by the backend as the
//! handshake start, then the subprocess's real signed `connect` would be
//! consumed as the "signed reply" — with the backend keying the eventual
//! `hello-ok` to the proxy's wake-up id instead of the subprocess's
//! request id. The subprocess would never see a response matching its
//! own pending request and the handshake would silently time out.
//! The backend now triggers `handle_node_connect` on `$connect` for any
//! node-host clientId, so no wake-up frame is needed to kick the
//! upstream container handshake into motion.

use futures_util::{SinkExt, StreamExt};
use std::sync::Arc;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::Message;

use crate::log;

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
                    let (stream, peer) = match accepted {
                        Ok(v) => v,
                        Err(e) => {
                            log(&format!("[proxy] listener.accept failed: {e}"));
                            continue;
                        }
                    };
                    log(&format!("[proxy] TCP accepted from {peer}"));
                    let upstream_url = upstream_url.clone();
                    let client_id = client_id.clone();
                    let jwt_provider = jwt_provider.clone();
                    tokio::spawn(async move {
                        let _ = handle_connection(stream, peer.to_string(), upstream_url, client_id, jwt_provider).await;
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
///  4. Spawn two halves of bidirectional frame forwarding.
///
/// No synthetic frames are injected. The first frame the backend sees on
/// this connection is the subprocess's signed `connect` request (after
/// the upstream container's `connect.challenge` arrives via Management
/// API). See module docstring for the protocol-correctness rationale.
async fn handle_connection(
    stream: TcpStream,
    peer: String,
    upstream_url: String,
    client_id: String,
    jwt_provider: JwtProvider,
) -> Result<(), String> {
    let sub_ws = match tokio_tungstenite::accept_async(stream).await {
        Ok(ws) => ws,
        Err(e) => {
            log(&format!("[proxy] downstream accept_async failed from {peer}: {e}"));
            return Err(format!("sub-side accept: {e}"));
        }
    };
    log(&format!("[proxy] downstream WS accepted from {peer}"));
    let (mut sub_tx, mut sub_rx) = sub_ws.split();

    let jwt = jwt_provider();
    log(&format!("[proxy] jwt_provider returned {} chars", jwt.len()));
    // urlencoding::encode is overkill here — Clerk JWTs are base64url and
    // clientId comes from us, but using `?` vs `&` correctly matters in
    // case the caller already embedded a query string in upstream_url.
    let separator = if upstream_url.contains('?') { '&' } else { '?' };
    let upstream_full = format!("{upstream_url}{separator}token={jwt}&clientId={client_id}");

    // For logging, strip ANY existing query string from upstream_url and
    // emit only scheme+host+path plus the redacted token/clientId we
    // append. If the backend ever returns a pre-signed gateway_url (or
    // any URL with credential-bearing query params), naive token-only
    // redaction would leak those verbatim. /tmp/isol8-desktop.log is
    // world-readable, so we only emit the parts WE control here.
    let upstream_base = upstream_url.split('?').next().unwrap_or(&upstream_url);
    log(&format!(
        "[proxy] upstream connect: {upstream_base}?token=***&clientId={client_id}"
    ));

    let upstream_ws = match tokio_tungstenite::connect_async(&upstream_full).await {
        Ok((ws, _resp)) => {
            log("[proxy] upstream WS open");
            ws
        }
        Err(e) => {
            log(&format!("[proxy] upstream connect_async failed: {e}"));
            return Err(format!("upstream connect: {e}"));
        }
    };
    let (mut up_tx, mut up_rx) = upstream_ws.split();

    // Bidirectional forwarding. Either side closing tears the bridge down.
    let sub_to_up = async {
        while let Some(msg) = sub_rx.next().await {
            let msg = match msg {
                Ok(m) => m,
                Err(e) => {
                    log(&format!("[proxy] sub_to_up read error: {e}"));
                    break;
                }
            };
            if matches!(msg, Message::Close(_)) {
                log("[proxy] sub_to_up: downstream sent Close, forwarding upstream");
                let _ = up_tx.send(msg).await;
                break;
            }
            if up_tx.send(msg).await.is_err() {
                log("[proxy] up_tx send failed");
                break;
            }
        }
        log("[proxy] sub_to_up loop exited");
    };
    let up_to_sub = async {
        while let Some(msg) = up_rx.next().await {
            let msg = match msg {
                Ok(m) => m,
                Err(e) => {
                    log(&format!("[proxy] up_to_sub read error: {e}"));
                    break;
                }
            };
            if matches!(msg, Message::Close(_)) {
                log("[proxy] up_to_sub: upstream sent Close, forwarding downstream");
                let _ = sub_tx.send(msg).await;
                break;
            }
            if sub_tx.send(msg).await.is_err() {
                log("[proxy] sub_tx send failed");
                break;
            }
        }
        log("[proxy] up_to_sub loop exited");
    };

    tokio::select! {
        _ = sub_to_up => {}
        _ = up_to_sub => {}
    }
    Ok(())
}
