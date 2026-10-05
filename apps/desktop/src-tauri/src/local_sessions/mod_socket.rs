//! The Claude Code mod's door to this app (spec 2026-10-04-session-sharing-mod §4.1). The mod
//! runs inside the `claude` process, so the peer pid IS the mirrored process: no parent step.
//! It reads the session's sharing state and sends the owner's terminal decisions. Answers only
//! a pid that is exactly one live, unrestricted, mirrored Claude session; anything else is 404.

use super::*;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

const MAX_REQUEST: usize = 8 * 1024;

pub(super) struct Caller {
    pub session_id: String,
    pub generation: String,
    pub terminal: Option<&'static str>,
    pub approval_waiting: bool,
}

/// The one mirrored, live Claude session whose process is `pid`.
fn resolve(scope: &Path, pid: u32) -> Option<Caller> {
    let me = process(pid).ok()?;
    let mut found = Vec::new();
    for entry in fs::read_dir(scope).ok()?.flatten() {
        let path = entry.path();
        let key = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        if path.extension().is_none_or(|e| e != "json") || key.len() != 64 || !key.bytes().all(|b| b.is_ascii_hexdigit()) {
            continue;
        }
        let Ok(mirror) = private_read::<Mirror>(&path) else { continue };
        if mirror.ended_at.is_some()
            || mirror.native.restriction.is_some()
            || mirror.native.provider != "claude"
            || mirror.native.process != me
        {
            continue;
        }
        if let Some(sid) = &mirror.session_id {
            found.push(Caller {
                session_id: sid.clone(),
                generation: mirror.generation.clone(),
                // The heartbeat's `terminal`, with its "none" as null.
                terminal: Some(mirror.native.terminal()).filter(|t| *t != "none"),
                // A prompt the hook holds open for a remote answer.
                approval_waiting: mirror.waiting.as_ref().is_some_and(|w| w.held.is_some()),
            });
        }
    }
    (found.len() == 1).then(|| found.remove(0))
}

pub(super) fn state_json(sid: &str, terminal: Option<&str>, presence: Option<presence::Entry>, approval_waiting: bool) -> Value {
    let p = presence.unwrap_or(presence::Entry { generation: String::new(), paused: false, viewers: vec![], join_requests: vec![], at: 0 });
    json!({"sessionId": sid, "paused": p.paused, "terminal": terminal,
           "viewers": p.viewers, "joinRequests": p.join_requests, "approvalWaiting": approval_waiting})
}

pub(super) fn decision_body(sid: &str, generation: &str, body: &Value) -> Result<Value> {
    let decision = body["decision"].as_str().filter(|d| ["allow", "decline", "pause", "unpause"].contains(d)).ok_or("invalid decision")?;
    let answers = matches!(decision, "allow" | "decline");
    let user = body["userId"].as_str().filter(|v| !v.is_empty() && v.len() <= 128);
    let request = body["requestId"].as_str().filter(|v| !v.is_empty() && v.len() <= 64);
    if answers != (user.is_some() && request.is_some()) {
        return Err("invalid decision".into());
    }
    let mut out = json!({"sessionId": sid, "generation": generation, "decision": decision});
    if answers {
        out["userId"] = json!(user);
        out["requestId"] = json!(request);
    }
    Ok(out)
}

/// An Allow or Decline whose request is no longer pending here: refused, so every accepted
/// press is one made while remote typing was frozen.
pub(super) fn stale(sid: &str, payload: &Value) -> bool {
    match (payload["decision"].as_str(), payload["userId"].as_str(), payload["requestId"].as_str()) {
        (Some("allow" | "decline"), Some(user), Some(request)) => !presence::pending(sid, payload["generation"].as_str().unwrap_or(""), user, request),
        (Some("allow" | "decline"), ..) => true,
        _ => false,
    }
}

pub(super) fn parse_request(raw: &[u8]) -> Option<(String, String, Value)> {
    let text = std::str::from_utf8(raw).ok()?;
    let (head, body) = text.split_once("\r\n\r\n")?;
    let mut parts = head.lines().next()?.split(' ');
    let (method, path) = (parts.next()?.to_owned(), parts.next()?.to_owned());
    let body = if body.is_empty() { Value::Null } else { serde_json::from_str(body).ok()? };
    Some((method, path, body))
}

async fn read_request(socket: &mut UnixStream) -> Option<(String, String, Value)> {
    let mut buf = Vec::with_capacity(1024);
    let mut chunk = [0u8; 1024];
    loop {
        let n = socket.read(&mut chunk).await.ok()?;
        if n == 0 {
            return parse_request(&buf);
        }
        buf.extend_from_slice(&chunk[..n]);
        if buf.len() > MAX_REQUEST {
            return None;
        }
        if let Some(at) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
            let head = std::str::from_utf8(&buf[..at]).ok()?;
            let length = head
                .lines()
                .find_map(|l| l.to_ascii_lowercase().strip_prefix("content-length:").map(|v| v.trim().parse::<usize>().ok()))
                .flatten()
                .unwrap_or(0);
            if length > MAX_REQUEST {
                return None;
            }
            if buf.len() >= at + 4 + length {
                return parse_request(&buf[..at + 4 + length]);
            }
        }
    }
}

async fn reply(socket: &mut UnixStream, status: &str, body: &Value) {
    let body = body.to_string();
    let _ = socket
        .write_all(format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).as_bytes())
        .await;
}

async fn answer(mut socket: UnixStream, scope: PathBuf, enrollment: Enrollment, identity: crate::keychain::DeviceIdentity, client: reqwest::Client) {
    let Some(caller) = native_tools::peer_pid(&socket).ok().and_then(|pid| resolve(&scope, pid)) else {
        return reply(&mut socket, "404 Not Found", &json!({"error":"not_shared"})).await;
    };
    let Ok(Some((method, path, body))) = tokio::time::timeout(Duration::from_secs(2), read_request(&mut socket)).await else {
        return reply(&mut socket, "400 Bad Request", &json!({"error":"invalid_request"})).await;
    };
    let state = |c: &Caller| state_json(&c.session_id, c.terminal, presence::get(&c.session_id, &c.generation), c.approval_waiting);
    match (method.as_str(), path.as_str()) {
        ("GET", "/v1/state") => reply(&mut socket, "200 OK", &state(&caller)).await,
        ("POST", "/v1/decide") => {
            let Ok(payload) = decision_body(&caller.session_id, &caller.generation, &body) else {
                return reply(&mut socket, "400 Bad Request", &json!({"error":"invalid_decision"})).await;
            };
            if stale(&caller.session_id, &payload) {
                return reply(&mut socket, "409 Conflict", &json!({"error":"stale","message":"That request is no longer waiting."})).await;
            }
            let Ok(token) = crate::device_token::mint_device_token(&identity.private_key_pem, &enrollment.device_id, &enrollment.owner_id, 90) else {
                return reply(&mut socket, "409 Conflict", &json!({"error":"unavailable","message":"Device authentication unavailable."})).await;
            };
            let url = format!("{}/api/v1/messaging/native-session-decisions", crate::LIGHTBULB_API_BASE.trim_end_matches('/'));
            let Ok(response) = client.post(url).bearer_auth(token).json(&payload).send().await else {
                return reply(&mut socket, "409 Conflict", &json!({"error":"unavailable","message":"Lightbulb is unreachable."})).await;
            };
            let ok = response.status().is_success();
            let data: Value = response.json().await.unwrap_or(Value::Null);
            if !ok {
                // `ApiProblem.body()`: a flat `{code, message, retryable, …}`.
                return reply(&mut socket, "409 Conflict", &json!({"error": data["code"], "message": data["message"]})).await;
            }
            // The backend pushes fresh presence after every decision; until it lands, apply this one.
            presence::decided(&caller.session_id, &payload, &data);
            reply(&mut socket, "200 OK", &state(&caller)).await
        }
        _ => reply(&mut socket, "404 Not Found", &json!({"error":"not_found"})).await,
    }
}

pub(super) async fn serve(scope: PathBuf, enrollment: Enrollment, identity: crate::keychain::DeviceIdentity) {
    let (listener, _guard) = match native_tools::bind("mod.sock") {
        Ok(value) => value,
        Err(reason) => {
            crate::log(&format!("[local-sessions] mod socket unavailable: {reason}"));
            return;
        }
    };
    let Ok(client) = reqwest::Client::builder().timeout(Duration::from_secs(8)).build() else {
        return;
    };
    loop {
        let Ok((socket, _)) = listener.accept().await else {
            break;
        };
        tokio::spawn(answer(socket, scope.clone(), enrollment.clone(), identity.clone(), client.clone()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mod_socket_parses_a_bounded_http_request() {
        let raw = b"POST /v1/decide HTTP/1.1\r\nHost: x\r\nContent-Length: 20\r\n\r\n{\"decision\":\"pause\"}";
        let (method, path, body) = parse_request(raw).unwrap();
        assert_eq!((method.as_str(), path.as_str()), ("POST", "/v1/decide"));
        assert_eq!(body["decision"], "pause");
        assert!(parse_request(b"GET /v1/state HTTP/1.1\r\n\r\n").is_some());
        assert!(parse_request(b"garbage").is_none());
    }

    #[test]
    fn mod_socket_state_shape_has_no_presence_until_the_backend_sends_it() {
        let state = state_json("11111111-1111-4111-8111-111111111111", Some("held"), None, false);
        assert_eq!(state["paused"], false);
        assert_eq!(state["viewers"], serde_json::json!([]));
        assert_eq!(state["terminal"], "held");
    }

    #[test]
    fn mod_socket_refuses_a_decision_body_that_names_no_request() {
        assert!(decision_body("sid", "g1", &serde_json::json!({"decision":"allow"})).is_err());
        assert!(decision_body("sid", "g1", &serde_json::json!({"decision":"reboot"})).is_err());
        let ok = decision_body("sid", "g1", &serde_json::json!({"decision":"allow","userId":"u","requestId":"r"})).unwrap();
        assert_eq!(ok["generation"], "g1");
    }

    #[test]
    fn mod_socket_resolves_no_session_for_a_pid_that_is_not_mirrored() {
        let dir = tempfile::tempdir().unwrap();
        assert!(resolve(dir.path(), std::process::id()).is_none());
    }

    #[test]
    fn mod_socket_resolves_the_mirrored_claude_process_itself() {
        let scope = tempfile::tempdir().unwrap();
        // test_mirror's process is this test process: the mod's peer IS the mirrored process.
        let mut mirror = super::super::transport::tests::test_mirror("11111111-1111-4111-8111-111111111111", "gen-1");
        mirror.native.provider = "claude".into();
        mirror.session_id = Some("session-a".into());
        let path = scope.path().join(format!("{}.json", "a".repeat(64)));
        mirror.persist(&path).unwrap();
        let caller = resolve(scope.path(), std::process::id()).unwrap();
        assert_eq!((caller.session_id.as_str(), caller.generation.as_str()), ("session-a", "gen-1"));
        assert!(!caller.approval_waiting);
        mirror.native.restriction = Some("restricted".into());
        mirror.persist(&path).unwrap();
        assert!(resolve(scope.path(), std::process::id()).is_none(), "a restricted session gets no door");
    }

    #[test]
    fn mod_socket_refuses_an_answer_to_a_request_that_is_not_pending() {
        let _g = presence::TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sid = "44444444-4444-4444-8444-444444444444";
        let allow = decision_body(sid, "g1", &json!({"decision":"allow","userId":"u2","requestId":"r2"})).unwrap();
        assert!(stale(sid, &allow), "no presence: nothing is frozen, so no answer is taken");
        presence::offer_frame(&json!({"sessionId":sid,"generation":"g1","paused":false,"viewers":[],
            "joinRequests":[{"userId":"u2","name":"Mira","requestId":"r2"}]}));
        assert!(!stale(sid, &allow));
        let other = decision_body(sid, "g1", &json!({"decision":"decline","userId":"u2","requestId":"r9"})).unwrap();
        assert!(stale(sid, &other), "an older request id is stale");
        let restarted = decision_body(sid, "g2", &json!({"decision":"allow","userId":"u2","requestId":"r2"})).unwrap();
        assert!(stale(sid, &restarted), "a request of the old generation cannot be answered by the new one");
        assert!(!stale(sid, &decision_body(sid, "g1", &json!({"decision":"pause"})).unwrap()));
    }
}
