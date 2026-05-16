//! Tests for the loopback WSS proxy (Task 18).
//!
//! Spins up a mock "upstream" WS server on a random port, points the
//! proxy at it, then connects a fake "subprocess" to the proxy's local
//! port and verifies:
//!   1. The proxy opens upstream with `?token=<jwt>&clientId=<id>` injected
//!      into the URL.
//!   2. The proxy sends a client-first `connect` frame upstream right after
//!      open — the wake-up dance that dodges API Gateway's GoneException race.
//!   3. Frames from the subprocess get forwarded upstream verbatim.

use futures_util::{SinkExt, StreamExt};
use isol8_desktop::proxy::start_proxy;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::{http, Message};
use tokio_tungstenite::{accept_hdr_async, connect_async};

/// Mock upstream WS server. Captures the request URI of each incoming
/// connection plus a transcript of every text frame it receives, and
/// echoes each frame back so the proxy's upstream→sub path stays alive.
async fn spawn_mock_upstream() -> (u16, Arc<Mutex<Option<String>>>, Arc<Mutex<Vec<String>>>) {
    let captured_uri: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let received: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();

    let cap = captured_uri.clone();
    let recv = received.clone();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let cap = cap.clone();
            let recv = recv.clone();
            tokio::spawn(async move {
                let ws = match accept_hdr_async(
                    stream,
                    |req: &http::Request<()>, resp: http::Response<()>| {
                        *cap.lock().unwrap() = Some(req.uri().to_string());
                        Ok(resp)
                    },
                )
                .await
                {
                    Ok(ws) => ws,
                    Err(_) => return,
                };
                let (mut tx, mut rx) = ws.split();
                while let Some(Ok(msg)) = rx.next().await {
                    if let Message::Text(t) = &msg {
                        recv.lock().unwrap().push(t.to_string());
                    }
                    let _ = tx.send(msg).await;
                }
            });
        }
    });

    (port, captured_uri, received)
}

#[tokio::test]
async fn proxy_injects_jwt_and_clientid_into_upstream_url() {
    let (upstream_port, captured_uri, _received) = spawn_mock_upstream().await;
    let upstream_url = format!("ws://127.0.0.1:{upstream_port}/");

    let handle = start_proxy(
        upstream_url,
        "node-host".to_string(),
        Arc::new(|| "test_jwt_xyz".to_string()),
    )
    .await
    .unwrap();
    let proxy_port = handle.local_port();

    // Fake subprocess connects to the proxy.
    let (mut sub_ws, _) = connect_async(format!("ws://127.0.0.1:{proxy_port}/"))
        .await
        .unwrap();

    // Give the proxy a moment to dial upstream.
    tokio::time::sleep(Duration::from_millis(300)).await;

    let url = captured_uri
        .lock()
        .unwrap()
        .clone()
        .expect("upstream connection never accepted");
    assert!(
        url.contains("token=test_jwt_xyz"),
        "missing token in upstream URI: {url}"
    );
    assert!(
        url.contains("clientId=node-host"),
        "missing clientId in upstream URI: {url}"
    );

    let _ = sub_ws.close(None).await;
    handle.shutdown().await;
}

#[tokio::test]
async fn proxy_sends_wake_up_connect_frame_first() {
    let (upstream_port, _captured_uri, received) = spawn_mock_upstream().await;
    let upstream_url = format!("ws://127.0.0.1:{upstream_port}/");

    let handle = start_proxy(
        upstream_url,
        "node-host".to_string(),
        Arc::new(|| "jwt".to_string()),
    )
    .await
    .unwrap();
    let proxy_port = handle.local_port();

    let (mut sub_ws, _) = connect_async(format!("ws://127.0.0.1:{proxy_port}/"))
        .await
        .unwrap();

    tokio::time::sleep(Duration::from_millis(300)).await;

    let frames = received.lock().unwrap().clone();
    assert!(
        !frames.is_empty(),
        "no frames forwarded upstream — wake-up missing"
    );
    let first: serde_json::Value =
        serde_json::from_str(&frames[0]).expect("first upstream frame not JSON");
    assert_eq!(
        first.get("method").and_then(|m| m.as_str()),
        Some("connect"),
        "first frame is not a connect: {}",
        frames[0]
    );
    assert_eq!(first["params"]["role"], "node");
    assert_eq!(first["params"]["client"]["id"], "node-host");

    let _ = sub_ws.close(None).await;
    handle.shutdown().await;
}

#[tokio::test]
async fn proxy_forwards_frames_bidirectionally_after_handshake() {
    let (upstream_port, _captured_uri, received) = spawn_mock_upstream().await;
    let upstream_url = format!("ws://127.0.0.1:{upstream_port}/");

    let handle = start_proxy(
        upstream_url,
        "node-host".to_string(),
        Arc::new(|| "jwt".to_string()),
    )
    .await
    .unwrap();
    let proxy_port = handle.local_port();

    let (mut sub_ws, _) = connect_async(format!("ws://127.0.0.1:{proxy_port}/"))
        .await
        .unwrap();

    // Give the upstream connect + wake-up frame time to land before the sub
    // sends anything, otherwise frame ordering is racy.
    tokio::time::sleep(Duration::from_millis(150)).await;

    sub_ws
        .send(Message::Text(r#"{"hello":"from sub"}"#.into()))
        .await
        .unwrap();

    tokio::time::sleep(Duration::from_millis(300)).await;

    let frames = received.lock().unwrap().clone();
    assert!(
        frames.iter().any(|f| f.contains("from sub")),
        "subprocess frame not forwarded upstream: {frames:?}"
    );

    let _ = sub_ws.close(None).await;
    handle.shutdown().await;
}
