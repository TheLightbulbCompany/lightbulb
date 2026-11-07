//! Tests for the loopback WSS proxy (Task 18).
//!
//! Spins up a mock "upstream" WS server on a random port, points the
//! proxy at it, then connects a fake "subprocess" to the proxy's local
//! port and verifies:
//!   1. The proxy opens upstream with `?token=<jwt>&clientId=<id>` injected
//!      into the URL.
//!   2. The proxy does NOT inject any synthetic frames upstream — it's a
//!      transparent relay. Codex PR #667 round-3 (P1) flagged a wake-up
//!      `connect` frame the proxy used to inject; it caused the backend
//!      to key `hello-ok` to the wake-up id, and the subprocess's own
//!      pending request never matched, so the CLI handshake silently
//!      timed out. The wake-up is gone; the backend now starts the
//!      handshake itself on `$connect` for node-host clientIds.
//!   3. Frames from the subprocess get forwarded upstream verbatim.

use futures_util::{SinkExt, StreamExt};
use lightbulb_desktop::proxy::start_proxy;
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
async fn proxy_does_not_inject_frames_before_downstream_input() {
    // Regression test for Codex PR #667 round-3 (P1): the proxy used to
    // synthesize a `connect` wake-up frame upstream the moment the
    // upstream WSS opened. That collided with the OpenClaw v3
    // server-initiated handshake — the backend treated the wake-up as
    // the unsigned start of the handshake, then misinterpreted the
    // subprocess's real (signed) `connect` as the signed reply, and
    // returned `hello-ok` keyed to the wake-up id instead of the
    // subprocess's pending request. The subprocess's pending-request
    // map never matched the response, so the CLI silently timed out.
    //
    // The fix is: no proxy-injected frames at all. The first frame on
    // the upstream socket must come from the downstream (subprocess)
    // side. The backend opens the upstream container WS itself in
    // `handle_node_connect` (kicked off on `$connect` for node-host
    // clientIds), which is what produces the real `connect.challenge`
    // the subprocess waits for.
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

    // Give the proxy plenty of time to dial upstream and to inject a
    // wake-up if it were still doing so. The historical race window for
    // wake-up emission was <50ms; 500ms is overkill but cheap.
    tokio::time::sleep(Duration::from_millis(500)).await;

    let frames = received.lock().unwrap().clone();
    assert!(
        frames.is_empty(),
        "proxy injected unsolicited frames upstream before downstream input: {frames:?}"
    );

    let _ = sub_ws.close(None).await;
    handle.shutdown().await;
}

#[tokio::test]
async fn proxy_forwards_downstream_frames_upstream_verbatim() {
    // After the wake-up was removed, the FIRST upstream frame must be
    // exactly what the subprocess sent — no leading synthetic frame.
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

    // Give the upstream connect time to complete so the relay is live
    // before we send a frame.
    tokio::time::sleep(Duration::from_millis(150)).await;

    sub_ws
        .send(Message::Text(r#"{"hello":"from sub"}"#.into()))
        .await
        .unwrap();

    tokio::time::sleep(Duration::from_millis(300)).await;

    let frames = received.lock().unwrap().clone();
    assert_eq!(frames.len(), 1, "expected exactly one upstream frame, got {frames:?}");
    assert!(
        frames[0].contains("from sub"),
        "subprocess frame not forwarded upstream verbatim: {frames:?}"
    );

    let _ = sub_ws.close(None).await;
    handle.shutdown().await;
}
