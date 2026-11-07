//! The Claude Code mod's door to this app (spec 2026-10-04-session-sharing-mod §4.1). The mod
//! runs inside the `claude` process, so the peer pid IS the mirrored process: no parent step.
//! It reads the session's sharing state and sends the owner's terminal decisions. Answers only
//! a pid that is exactly one live, unrestricted, mirrored Claude session; anything else is 404.
//! One route more, `POST /v1/share`, shares a session and also takes a peer that the session's
//! process started (the share skill's `curl`), for Claude Code and Codex alike.

use super::*;
use serde_json::{json, Value};
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

const MAX_REQUEST: usize = 8 * 1024;

pub(super) struct Caller {
    /// Its mirror's key: what a share or a stop names (`transport::ask_share`).
    pub key: String,
    pub session_id: String,
    pub generation: String,
    pub terminal: Option<&'static str>,
    pub approval_waiting: bool,
}

/// What the band of a Claude session shows.
pub(super) enum Band {
    /// Shared and registered.
    Shared(Caller),
    /// Only its person's: nothing of it leaves this Mac.
    OnlyYou,
    /// Its person stopped sharing it and nothing leaves this Mac any more, but the workspace
    /// has not taken the stop yet: teammates may still see what was shared before.
    Stopping,
}

/// Every stored mirror with its key. A share or a stop the relay loop has not applied yet
/// already counts, so an answer is never the moment before the person's own press.
pub(super) fn mirrors(scope: &Path) -> Vec<(String, Mirror)> {
    fs::read_dir(scope)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|entry| {
            let path = entry.path();
            let key = path.file_stem()?.to_str()?.to_owned();
            if path.extension().is_none_or(|e| e != "json") || key.len() != 64 || !key.bytes().all(|b| b.is_ascii_hexdigit()) {
                return None;
            }
            let mut mirror = private_read::<Mirror>(&path).ok()?;
            if let Some((shared, owed)) = super::transport::asked(&key) {
                (mirror.shared, mirror.stop_owed) = (shared, owed);
            }
            if super::transport::is_settled(&key) {
                mirror.stop_owed = false;
            }
            Some((key, mirror))
        })
        .collect()
}

/// The one mirrored, live Claude session whose process is `pid`.
fn resolve(scope: &Path, pid: u32) -> Option<Band> {
    let me = process(pid).ok()?;
    let mut found: Vec<Band> = mirrors(scope)
        .into_iter()
        .filter(|(_, m)| m.ended_at.is_none() && m.native.restriction.is_none() && m.native.provider == "claude" && m.native.process == me)
        .filter_map(|(key, mirror)| {
            if !mirror.shared {
                return Some(if mirror.stop_owed && mirror.session_id.is_some() { Band::Stopping } else { Band::OnlyYou });
            }
            Some(Band::Shared(Caller {
                key,
                session_id: mirror.session_id.clone()?,
                generation: mirror.generation.clone(),
                // The heartbeat's `terminal`, with its "none" as null.
                terminal: Some(mirror.native.terminal()).filter(|t| *t != "none"),
                // A prompt the hook holds open for a remote answer.
                approval_waiting: mirror.waiting.as_ref().is_some_and(|w| w.held.is_some()),
            }))
        })
        .collect();
    (found.len() == 1).then(|| found.remove(0))
}

/// The peer and the processes above it, nearest first. The band runs in the session's own
/// process; the share skill's `curl` is started by it, through a shell.
fn lineage(pid: u32) -> Vec<Process> {
    let mut line: Vec<Process> = process(pid).into_iter().collect();
    while line.len() < 6 {
        let Some(above) = line.last().and_then(|p| native_tools::parent(p.pid).ok()) else { break };
        line.push(above);
    }
    line
}

/// The live session `POST /v1/share` shares: the one whose process the peer is or was started
/// by. That is the whole authority. `named` (a Claude session id, a Codex thread id) only
/// chooses when that process runs several (a Codex app or daemon hosts many threads).
fn to_share(scope: &Path, pid: u32, named: Option<&str>) -> Option<(String, Mirror)> {
    let line = lineage(pid);
    let mut found: Vec<(String, Mirror)> =
        mirrors(scope).into_iter().filter(|(_, m)| m.ended_at.is_none() && line.contains(&m.native.process)).collect();
    if found.len() > 1 {
        found.retain(|(_, m)| named == Some(m.native.conversation.as_str()));
    }
    (found.len() == 1).then(|| found.remove(0))
}

/// The generation of `mirror` the relay knows: a decision names that one (a session revived
/// while it was not shared runs under one that never registered).
fn known_generation(mirror: &Mirror) -> &str {
    mirror.registration.acknowledged.as_deref().unwrap_or(&mirror.generation)
}

/// The stops this Mac still owes the workspace, as (mirror key, session id, generation).
fn owed_stops(scope: &Path) -> Vec<(String, String, String)> {
    mirrors(scope)
        .into_iter()
        .filter(|(_, m)| !m.shared && m.stop_owed)
        .filter_map(|(key, m)| Some((key, m.session_id.clone()?, known_generation(&m).to_owned())))
        .collect()
}

pub(super) fn state_json(sid: &str, terminal: Option<&str>, presence: Option<presence::Entry>, approval_waiting: bool) -> Value {
    let p = presence.unwrap_or(presence::Entry { generation: String::new(), paused: false, viewers: vec![], join_requests: vec![], link_viewers: 0, at: 0 });
    json!({"sessionId": sid, "paused": p.paused, "terminal": terminal,
           "viewers": p.viewers, "linkViewers": p.link_viewers, "joinRequests": p.join_requests, "approvalWaiting": approval_waiting})
}

pub(super) fn decision_body(sid: &str, generation: &str, body: &Value) -> Result<Value> {
    let decision = body["decision"].as_str().filter(|d| ["allow", "decline", "pause", "unpause"].contains(d)).ok_or("invalid decision")?;
    let answers = matches!(decision, "allow" | "decline");
    let user = body["userId"].as_str().filter(|v| !v.is_empty() && v.len() <= 128);
    // An agent's ask id is an `OpaqueId`, up to 256 characters (spec 2026-10-07-agent-session-approval).
    let request = body["requestId"].as_str().filter(|v| !v.is_empty() && v.len() <= 256);
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

/// An Allow or Decline whose request is no longer pending here: refused, so a press can only
/// answer the request the band showed.
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

/// One decision sent to the backend as this Mac. Err is the body the mod is answered with.
async fn decide(client: &reqwest::Client, enrollment: &Enrollment, identity: &crate::keychain::DeviceIdentity, sid: &str, payload: &Value) -> std::result::Result<(), Value> {
    let Ok(token) = crate::device_token::mint_device_token(&identity.private_key_pem, &enrollment.device_id, &enrollment.owner_id, 90) else {
        return Err(json!({"error":"unavailable","message":"Device authentication unavailable."}));
    };
    let url = format!("{}/api/v1/messaging/native-session-decisions", crate::LIGHTBULB_API_BASE.trim_end_matches('/'));
    let Ok(response) = client.post(url).bearer_auth(token).json(payload).send().await else {
        return Err(json!({"error":"unavailable","message":"Lightbulb is unreachable."}));
    };
    let ok = response.status().is_success();
    let data: Value = response.json().await.unwrap_or(Value::Null);
    if !ok {
        // `ApiProblem.body()`: a flat `{code, message, retryable, …}`.
        return Err(json!({"error": data["code"], "message": data["message"]}));
    }
    // The backend pushes fresh presence after every decision; until it lands, apply this one.
    presence::decided(sid, payload, &data);
    Ok(())
}

/// Shares mirror `key`. One the workspace has had is paused there: it is resumed there FIRST.
/// The server drops what a paused session uploads, so nothing is released here until it has
/// taken the resume; a refusal is the caller's answer and the session stays as it was.
pub(super) async fn share(client: &reqwest::Client, enrollment: &Enrollment, identity: &crate::keychain::DeviceIdentity, key: &str, mirror: &Mirror) -> std::result::Result<(), Value> {
    if let Some(sid) = &mirror.session_id {
        let resume = json!({"sessionId": sid, "generation": known_generation(mirror), "decision": "unpause"});
        decide(client, enrollment, identity, sid, &resume).await?;
    }
    // The whole session, from its start: nothing of it was read while it was not shared.
    super::transport::ask_share(key, true);
    Ok(())
}

async fn answer(mut socket: UnixStream, scope: PathBuf, enrollment: Enrollment, identity: crate::keychain::DeviceIdentity, client: reqwest::Client) {
    let pid = native_tools::peer_pid(&socket).ok();
    let band = pid.and_then(|pid| resolve(&scope, pid));
    let Ok(Some((method, path, body))) = tokio::time::timeout(Duration::from_secs(2), read_request(&mut socket)).await else {
        return reply(&mut socket, "400 Bad Request", &json!({"error":"invalid_request"})).await;
    };
    let route = (method.as_str(), path.as_str());
    if route == ("POST", "/v1/share") {
        let named = body["sessionId"].as_str().filter(|id| !id.is_empty() && id.len() <= 128);
        let Some((key, mirror)) = pid.and_then(|pid| to_share(&scope, pid, named)) else {
            return reply(&mut socket, "404 Not Found", &json!({"error":"unknown_session","message":"Lightbulb does not see this session yet. Wait a few seconds, then try again."})).await;
        };
        if let Err(refusal) = share(&client, &enrollment, &identity, &key, &mirror).await {
            return reply(&mut socket, "409 Conflict", &refusal).await;
        }
        // `waiting` is always false now: a session with no live terminal is shared as its
        // conversation (owner ruling 2026-10-11). Kept for plugins that still read it.
        return reply(&mut socket, "200 OK", &json!({"shared": true, "waiting": false})).await;
    }
    let caller = match band {
        Some(Band::Shared(caller)) => caller,
        // Not shared (or stopping): the band says which, and there is nothing to decide.
        Some(local) if route == ("GET", "/v1/state") => {
            return reply(&mut socket, "200 OK", &json!({"shared": false, "stopping": matches!(local, Band::Stopping)})).await;
        }
        _ => return reply(&mut socket, "404 Not Found", &json!({"error":"not_shared"})).await,
    };
    let state = |c: &Caller| state_json(&c.session_id, c.terminal, presence::get(&c.session_id, &c.generation), c.approval_waiting);
    match route {
        ("GET", "/v1/state") => {
            let mut out = state(&caller);
            // `typedBy`: the teammate whose keys were the session's last input, so the mod can
            // label a prompt they submitted. The holder is asked only once a teammate has typed.
            if super::remote_input::has_typist(&caller.session_id) {
                let sid = caller.session_id.clone();
                let last_input = tokio::task::spawn_blocking(move || super::remote_input::holder_last_input(&sid)).await.ok().flatten();
                if let Some((user, name)) = super::remote_input::typed_by(&caller.session_id, std::time::Instant::now(), last_input.as_deref()) {
                    out["typedBy"] = json!({"userId": user, "name": name});
                }
            }
            reply(&mut socket, "200 OK", &out).await
        }
        ("POST", "/v1/decide") => {
            let Ok(payload) = decision_body(&caller.session_id, &caller.generation, &body) else {
                return reply(&mut socket, "400 Bad Request", &json!({"error":"invalid_decision"})).await;
            };
            // Only the owner operates the band (spec §2.4): a teammate's `a`/`d`/`p` reaches Claude
            // as typing, so the holder says whose input came last.
            let sid = caller.session_id.clone();
            let last_input = tokio::task::spawn_blocking(move || super::remote_input::holder_last_input(&sid)).await.ok().flatten();
            if super::remote_input::teammate_holds_decision(&caller.session_id, std::time::Instant::now(), last_input.as_deref()) {
                return reply(&mut socket, "409 Conflict", &json!({"error":"teammate_typing","message":"A teammate just typed here. Press again to confirm."})).await;
            }
            if stale(&caller.session_id, &payload) {
                return reply(&mut socket, "409 Conflict", &json!({"error":"stale","message":"That request is no longer waiting."})).await;
            }
            // Stopping is this Mac's own first, whatever the backend then answers: from here
            // nothing of the session leaves it. (Its terminal relay now, the rest within a turn of the relay loop.)
            let stop = payload["decision"] == "pause";
            if stop {
                super::transport::ask_share(&caller.key, false);
                super::screen::revoke(&caller.session_id);
            }
            let taken = decide(&client, &enrollment, &identity, &caller.session_id, &payload).await;
            if stop {
                // The stop stands on this Mac whatever the backend answered. One it did not
                // take stays owed (`serve` retries it), and the band says so, not "Only you".
                if taken.is_ok() {
                    super::transport::stop_settled(&caller.key);
                }
                return reply(&mut socket, "200 OK", &json!({"shared": false, "stopping": taken.is_err()})).await;
            }
            if let Err(refusal) = taken {
                return reply(&mut socket, "409 Conflict", &refusal).await;
            }
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
    // A stop the workspace has not taken (the API was down, the token expired) is owed until it
    // does: tried again here, also after its session's own process is gone.
    let mut retry = tokio::time::interval(Duration::from_secs(10));
    loop {
        tokio::select! {
            accepted = listener.accept() => {
                let Ok((socket, _)) = accepted else { break };
                tokio::spawn(answer(socket, scope.clone(), enrollment.clone(), identity.clone(), client.clone()));
            }
            _ = retry.tick() => {
                let (owed, enrollment, identity, client) = (owed_stops(&scope), enrollment.clone(), identity.clone(), client.clone());
                tokio::spawn(async move {
                    for (key, sid, generation) in owed {
                        let pause = json!({"sessionId": sid, "generation": generation, "decision": "pause"});
                        if decide(&client, &enrollment, &identity, &sid, &pause).await.is_ok() {
                            super::transport::stop_settled(&key);
                        }
                    }
                });
            }
        }
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
        let conversation = "11111111-1111-4111-8111-111111111111";
        let mut mirror = super::super::transport::tests::test_mirror(conversation, "gen-1");
        mirror.native.provider = "claude".into();
        mirror.session_id = Some("session-a".into());
        let key = "a".repeat(64);
        let path = scope.path().join(format!("{key}.json"));
        mirror.persist(&path).unwrap();
        let Some(Band::Shared(caller)) = resolve(scope.path(), std::process::id()) else { panic!("the shared band") };
        assert_eq!((caller.session_id.as_str(), caller.generation.as_str(), caller.key.as_str()), ("session-a", "gen-1", key.as_str()));
        assert!(!caller.approval_waiting);
        // Shared, in a terminal this Mac cannot show: shared as its conversation, with no terminal.
        mirror.native.app = false;
        mirror.persist(&path).unwrap();
        assert!(matches!(resolve(scope.path(), std::process::id()), Some(Band::Shared(c)) if c.terminal.is_none()));
        // Only its person's: registered before or never, the band offers Share to Lightbulb.
        mirror.shared = false;
        mirror.session_id = None;
        mirror.persist(&path).unwrap();
        assert!(matches!(resolve(scope.path(), std::process::id()), Some(Band::OnlyYou)));
        // Stopped here, and the workspace has not taken the stop: not yet "Only you". It is
        // owed under the generation the relay knows.
        mirror.session_id = Some("session-a".into());
        mirror.stop_owed = true;
        mirror.registration.acknowledged = Some("gen-0".into());
        mirror.persist(&path).unwrap();
        assert!(matches!(resolve(scope.path(), std::process::id()), Some(Band::Stopping)));
        assert_eq!(owed_stops(scope.path()), vec![(key.clone(), "session-a".to_string(), "gen-0".to_string())]);
        mirror.native.restriction = Some("restricted".into());
        mirror.persist(&path).unwrap();
        assert!(resolve(scope.path(), std::process::id()).is_none(), "a restricted session gets no door");
    }

    #[test]
    fn mod_socket_share_takes_the_session_s_own_process_or_one_it_started() {
        let scope = tempfile::tempdir().unwrap();
        let conversation = "22222222-2222-4222-8222-222222222222";
        let mut mirror = super::super::transport::tests::test_mirror(conversation, "gen-1");
        mirror.shared = false;
        let key = "5".repeat(64); // no other test holds this key: the asks below are one static
        mirror.persist(&scope.path().join(format!("{key}.json"))).unwrap();
        // The band: the session's own process. The skill: a process it started.
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let share = |pid: u32, named: Option<&str>| to_share(scope.path(), pid, named).map(|(key, _)| key);
        let own = share(std::process::id(), Some(conversation));
        // The one session of that process, whatever the peer calls it (a skill whose id was not filled in).
        let started = (share(child.id(), None), share(child.id(), Some("${CLAUDE_SESSION_ID}")));
        // A process this session did not start (launchd) shares nothing, whatever it names.
        let stranger = share(1, Some(conversation));
        // A process that runs two (a Codex app or daemon): only the named one.
        let other = "33333333-3333-4333-8333-333333333333";
        let second = "6".repeat(64);
        super::super::transport::tests::test_mirror(other, "gen-1").persist(&scope.path().join(format!("{second}.json"))).unwrap();
        let of_two = (share(child.id(), None), share(child.id(), Some(other)));
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(own.as_deref(), Some(key.as_str()));
        assert_eq!(started, (Some(key.clone()), Some(key.clone())));
        assert_eq!(stranger, None);
        assert_eq!(of_two, (None, Some(second)));
        // An ask the relay loop has not applied yet is already the answer. A stop is owed to
        // the workspace until it has taken it: only then is the session "Only you".
        let state = || mirrors(scope.path()).into_iter().find(|(k, _)| *k == key).map(|(_, m)| (m.shared, m.stop_owed));
        assert_eq!(state(), Some((false, false)));
        super::super::transport::ask_share(&key, true);
        assert_eq!(state(), Some((true, false)));
        super::super::transport::ask_share(&key, false);
        assert_eq!(state(), Some((false, true)));
        assert!(owed_stops(scope.path()).is_empty(), "the relay never had it: nothing to tell");
        super::super::transport::stop_settled(&key);
        assert_eq!(state(), Some((false, false)));
    }

    #[test]
    fn mod_socket_refuses_an_answer_to_a_request_that_is_not_pending() {
        let _g = presence::TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sid = "44444444-4444-4444-8444-444444444444";
        let allow = decision_body(sid, "g1", &json!({"decision":"allow","userId":"u2","requestId":"r2"})).unwrap();
        assert!(stale(sid, &allow), "no presence: no request is pending, so no answer is taken");
        presence::offer_frame(&json!({"sessionId":sid,"generation":"g1","paused":false,"viewers":[],
            "joinRequests":[{"userId":"u2","name":"Mira","requestId":"r2"}]}));
        assert!(!stale(sid, &allow));
        let other = decision_body(sid, "g1", &json!({"decision":"decline","userId":"u2","requestId":"r9"})).unwrap();
        assert!(stale(sid, &other), "an older request id is stale");
        let restarted = decision_body(sid, "g2", &json!({"decision":"allow","userId":"u2","requestId":"r2"})).unwrap();
        assert!(stale(sid, &restarted), "a request of the old generation cannot be answered by the new one");
        assert!(!stale(sid, &decision_body(sid, "g1", &json!({"decision":"pause"})).unwrap()));
    }

    #[test]
    fn mod_socket_takes_a_256_character_ask_id_and_refuses_a_longer_one() {
        let allow = decision_body("s", "g1", &json!({"decision":"allow","userId":"agent-friday","requestId":"a".repeat(256)})).unwrap();
        assert_eq!(allow["requestId"].as_str().map(str::len), Some(256));
        assert!(decision_body("s", "g1", &json!({"decision":"allow","userId":"agent-friday","requestId":"a".repeat(257)})).is_err());
    }
}
