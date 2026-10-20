//! A Mac notification for each request waiting on this Mac's person (Task 7b, agent network §6):
//! a teammate whose session send is held until they are allowed, or an agent's ask. Its Allow
//! and Decline answer through the same device-signed decision the Claude band sends. Only a
//! person presses a notification action: no tool, MCP call or command an agent can run answers
//! one. The process that owns local sessions (`serve`, or the window when it serves) posts the
//! notifications; the one macOS hands a press to answers it from the notification's own
//! `userInfo` (`on_action`), serving or not.
//!
//! The agent in the target session hears about the request once, as one line from its own hook
//! (`notice_line`), and is never given a way to answer it.

use super::*;
use crate::notify::{Center, Notice};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Mutex;
use std::time::Duration;

const PREVIEW_CHARS: usize = 140;
pub(super) const FAILED: &str = "Could not send. Try again.";

/// A session this Mac shares, as a notification names it.
pub(super) struct Session {
    pub sid: String,
    pub generation: String,
    pub title: String,
}

/// The Mac a decision is signed as: (owner id, device id).
pub(super) type Mac<'a> = (&'a str, &'a str);

fn id_of(request_id: &str) -> String {
    format!("lightbulb.request.{request_id}")
}

/// "Aden's Codex" (the label delivery uses), "Friday" (an agent), else the requester's name.
fn who(r: &presence::JoinRequest) -> &str {
    r.label.as_deref().filter(|l| !l.is_empty()).unwrap_or(&r.name)
}

/// Requests a notification answers: a person's join request with a held send (it carries the
/// send's text), or an agent's ask. A plain join or a pick-up stays on the band.
fn notifies(r: &presence::JoinRequest) -> bool {
    r.kind == "agent_message" || (r.kind == "join" && r.text.is_some())
}

fn preview(text: &str) -> String {
    let text = text.trim();
    if text.chars().count() <= PREVIEW_CHARS {
        return text.to_owned();
    }
    let mut out: String = text.chars().take(PREVIEW_CHARS - 1).collect();
    out.push('…');
    out
}

/// One notification per pending request, in `sessions` as presence holds them.
pub(super) fn wanted(sessions: &[Session], presence: impl Fn(&str, &str) -> Option<presence::Entry>, mac: Mac) -> Vec<Notice> {
    let mut out = vec![];
    for s in sessions {
        let Some(entry) = presence(&s.sid, &s.generation) else { continue };
        for r in entry.join_requests.iter().filter(|r| notifies(r)) {
            // The whole decision rides along: whichever process gets the press sends it.
            let info = [
                ("sessionId", s.sid.as_str()),
                ("generation", s.generation.as_str()),
                ("userId", r.user_id.as_str()),
                ("requestId", r.request_id.as_str()),
                ("ownerId", mac.0),
                ("deviceId", mac.1),
            ];
            out.push(Notice {
                id: id_of(&r.request_id),
                title: format!("{} wants to message {}", who(r), s.title),
                body: preview(r.text.as_deref().unwrap_or("")),
                info: info.into_iter().map(|(k, v)| (k.to_owned(), v.to_owned())).collect(),
            });
        }
    }
    out
}

/// The notifications this process has up.
#[derive(Default)]
pub(super) struct Tracker {
    posted: BTreeMap<String, Notice>,
}

impl Tracker {
    /// Posts each new request once (a repeat of an id is not news) and withdraws each one that
    /// was answered anywhere or left presence.
    pub fn sync(&mut self, wanted: Vec<Notice>, center: &dyn Center) {
        let ids: BTreeSet<String> = wanted.iter().map(|n| n.id.clone()).collect();
        let gone: Vec<String> = self.posted.keys().filter(|id| !ids.contains(*id)).cloned().collect();
        if !gone.is_empty() {
            center.withdraw(&gone);
            self.posted.retain(|id, _| ids.contains(id));
        }
        for n in wanted {
            if !self.posted.contains_key(&n.id) {
                center.post(&n);
                self.posted.insert(n.id.clone(), n);
            }
        }
    }

    fn all(&self) -> Vec<String> {
        self.posted.keys().cloned().collect()
    }
}

/// The notification again, after a decision the backend did not take.
pub(super) fn failed(n: &Notice) -> Notice {
    Notice { body: FAILED.into(), ..n.clone() }
}

/// What a press sends, from the notification alone: the Mac to sign as, and the band's body.
/// None for anything but Allow or Decline, or a notification that lacks part of it.
pub(super) fn decision_request(info: &BTreeMap<String, String>, action: &str) -> Option<(String, String, Value)> {
    if action != crate::notify::ALLOW && action != crate::notify::DECLINE {
        return None;
    }
    let get = |k: &str| info.get(k).map(String::as_str).filter(|v| !v.is_empty());
    let body = mod_socket::decision_body(
        get("sessionId")?,
        get("generation")?,
        &json!({"decision": action, "userId": get("userId")?, "requestId": get("requestId")?}),
    )
    .ok()?;
    Some((get("ownerId")?.to_owned(), get("deviceId")?.to_owned(), body))
}

/// Sends a press's decision to `api` as this Mac (`identity`), the band's device-signed route.
pub(super) async fn post_decision(api: &str, identity: &crate::keychain::DeviceIdentity, info: &BTreeMap<String, String>, action: &str) -> std::result::Result<(), String> {
    let (owner, device, body) = decision_request(info, action).ok_or("incomplete notification")?;
    let token = crate::device_token::mint_device_token(&identity.private_key_pem, &device, &owner, 90).map_err(|_| "device authentication unavailable")?;
    let client = reqwest::Client::builder().timeout(Duration::from_secs(8)).build().map_err(|e| e.to_string())?;
    let url = format!("{}/api/v1/messaging/native-session-decisions", api.trim_end_matches('/'));
    let response = client.post(url).bearer_auth(token).json(&body).send().await.map_err(|_| "Lightbulb is unreachable")?;
    if response.status().is_success() {
        Ok(())
    } else {
        Err(format!("refused: {}", response.status()))
    }
}

/// A press on Allow or Decline, in whichever process macOS handed it to: the window or `serve`,
/// serving or not. Signed with this Mac's Keychain key, read only (never generated). Taken: the
/// banner goes, and the serving process's next presence (the request gone) agrees. Not taken:
/// the same banner again, saying so.
pub(crate) fn on_action(notice: Notice, action: String) {
    tauri::async_runtime::spawn(async move {
        let center = crate::notify::Native;
        let taken = match crate::keychain::load(crate::keychain::KeychainSlot::Mac, crate::KEYCHAIN_SERVICE) {
            Ok(identity) => post_decision(crate::LIGHTBULB_API_BASE, &identity, &notice.info, &action).await,
            Err(_) => Err("no device key".into()),
        };
        match taken {
            Ok(()) => center.withdraw(std::slice::from_ref(&notice.id)),
            Err(reason) => {
                crate::log(&format!("[local-sessions] notification decision not taken: {reason}"));
                center.post(&failed(&notice));
            }
        }
    });
}

/// The session's own agent is told about a request once: its hook's one line.
pub(super) fn notice_line(who: &str) -> String {
    format!("Lightbulb: {who} wants to message this session; your user can allow it from the Mac notification or the Lightbulb app.")
}

/// Request ids an agent has been told about already.
static TOLD: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());

/// The lines for `sid`'s requests its agent has not been told about yet, each marked told.
pub(super) fn untold(sid: &str, generation: &str) -> Vec<String> {
    let Some(entry) = presence::get(sid, generation) else { return vec![] };
    let Ok(mut told) = TOLD.lock() else { return vec![] };
    entry.join_requests.iter().filter(|r| notifies(r) && told.insert(r.request_id.clone())).map(|r| notice_line(who(r))).collect()
}

/// Codex's PostToolUse answer carrying the lines: context for the agent, never a decision.
pub(super) fn hook_output(lines: &[String]) -> Option<Value> {
    (!lines.is_empty()).then(|| json!({"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext": lines.join("\n")}}))
}

/// From a hook process: the lines the app has for `session`, asked on its mod socket (which
/// names the session by the asking process). Nothing when the app is not running.
pub(super) fn ask_app(sock: &Path, session: &str) -> Vec<String> {
    use std::io::{Read, Write};
    let Ok(mut stream) = std::os::unix::net::UnixStream::connect(sock) else { return vec![] };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
    let body = json!({"sessionId": session}).to_string();
    if stream.write_all(format!("POST /v1/notice HTTP/1.1\r\nContent-Length: {}\r\n\r\n{body}", body.len()).as_bytes()).is_err() {
        return vec![];
    }
    let mut reply = Vec::new();
    let _ = stream.take(64 * 1024).read_to_end(&mut reply);
    let text = String::from_utf8_lossy(&reply);
    let Some((_, body)) = text.split_once("\r\n\r\n") else { return vec![] };
    serde_json::from_str::<Value>(body).ok().and_then(|v| serde_json::from_value(v["lines"].clone()).ok()).unwrap_or_default()
}

/// What this Mac shares, named as the band names it.
fn sessions(scope: &Path) -> Vec<Session> {
    mod_socket::mirrors(scope)
        .into_iter()
        .filter(|(_, m)| m.ended_at.is_none() && m.shared)
        .filter_map(|(_, m)| {
            let title = m.native.title.clone().or_else(|| m.native.cwd_name.clone()).filter(|t| !t.trim().is_empty());
            Some(Session { sid: m.session_id.clone()?, generation: m.generation.clone(), title: title.unwrap_or_else(|| "your session".into()) })
        })
        .collect()
}

/// Withdraws what is up when local sessions stop: an action nobody can take must not stay.
struct Live {
    tracker: Tracker,
    center: Box<dyn Center>,
}

impl Drop for Live {
    fn drop(&mut self) {
        self.center.withdraw(&self.tracker.all());
    }
}

pub(super) async fn run(scope: PathBuf, enrollment: Enrollment) {
    let mut live = Live { tracker: Tracker::default(), center: Box::new(crate::notify::Native) };
    loop {
        let wanted = wanted(&sessions(&scope), presence::get, (&enrollment.owner_id, &enrollment.device_id));
        live.tracker.sync(wanted, &*live.center);
        presence::changed().await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[derive(Clone, Default)]
    struct Fake(Arc<Mutex<Vec<String>>>);

    impl Center for Fake {
        fn post(&self, n: &Notice) {
            self.0.lock().unwrap().push(format!("post {} | {} | {}", n.id, n.title, n.body));
        }
        fn withdraw(&self, ids: &[String]) {
            self.0.lock().unwrap().push(format!("withdraw {}", ids.join(",")));
        }
    }

    impl Fake {
        fn take(&self) -> Vec<String> {
            std::mem::take(&mut *self.0.lock().unwrap())
        }
    }

    const SID: &str = "77777777-7777-4777-8777-777777777777";
    const MAC: Mac = ("org_a", "mac_a");

    fn session() -> Vec<Session> {
        vec![Session { sid: SID.into(), generation: "g1".into(), title: "Fix login".into() }]
    }

    fn entry(requests: Value) -> presence::Entry {
        serde_json::from_value(json!({"generation":"g1","paused":false,"viewers":[],"joinRequests":requests})).unwrap()
    }

    fn held() -> Value {
        json!({"userId":"user_aden","name":"Aden","requestId":"r1","kind":"join","text":"Can you rebase on main?","label":"Aden's Codex"})
    }

    fn ask() -> Value {
        json!({"userId":"agent-friday","name":"Friday","requestId":"ask-1","kind":"agent_message","action":"send","text":"Run the tests"})
    }

    fn of(e: presence::Entry) -> impl Fn(&str, &str) -> Option<presence::Entry> {
        move |sid, g| (sid == SID && g == e.generation).then(|| e.clone())
    }

    fn notices(requests: Value) -> Vec<Notice> {
        wanted(&session(), of(entry(requests)), MAC)
    }

    #[test]
    fn held_notice_maps_a_held_send_and_an_agent_ask_and_skips_plain_joins_and_pickups() {
        let plain = json!({"userId":"user_zoe","name":"Zoe","requestId":"r2","kind":"join"});
        let pickup = json!({"userId":"user_al","name":"Al","requestId":"p1","kind":"pickup"});
        let out = notices(json!([held(), ask(), plain, pickup]));
        let shown: Vec<_> = out.iter().map(|n| (n.id.as_str(), n.title.as_str(), n.body.as_str())).collect();
        assert_eq!(shown, vec![
            ("lightbulb.request.r1", "Aden's Codex wants to message Fix login", "Can you rebase on main?"),
            ("lightbulb.request.ask-1", "Friday wants to message Fix login", "Run the tests"),
        ]);
        // The notification carries its whole decision, and the Mac to sign it as.
        let info: Vec<_> = out[0].info.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        assert_eq!(info, vec![("deviceId", "mac_a"), ("generation", "g1"), ("ownerId", "org_a"), ("requestId", "r1"), ("sessionId", SID), ("userId", "user_aden")]);
        // No label from the backend: the requester's own name.
        let mut bare = held();
        bare.as_object_mut().unwrap().remove("label");
        assert_eq!(notices(json!([bare]))[0].title, "Aden wants to message Fix login");
        // Another generation of the session holds nothing for this one.
        assert!(wanted(&[Session { generation: "g2".into(), ..session().remove(0) }], of(entry(json!([held()]))), MAC).is_empty());
    }

    #[test]
    fn held_notice_body_is_at_most_140_characters() {
        let mut long = held();
        long["text"] = json!("é".repeat(300));
        let body = notices(json!([long]))[0].body.clone();
        assert_eq!(body.chars().count(), 140);
        assert!(body.ends_with('…'));
    }

    #[test]
    fn held_notice_posts_once_per_request_and_withdraws_when_it_is_answered_or_gone() {
        let center = Fake::default();
        let mut tracker = Tracker::default();
        tracker.sync(notices(json!([held(), ask()])), &center);
        assert_eq!(center.take().len(), 2);
        // The same request ids again (another presence push): nothing re-posted.
        tracker.sync(notices(json!([held(), ask()])), &center);
        assert!(center.take().is_empty());
        // The held send was answered elsewhere: its banner is withdrawn, the ask stays.
        tracker.sync(notices(json!([ask()])), &center);
        assert_eq!(center.take(), vec!["withdraw lightbulb.request.r1"]);
        // The session left presence: everything goes.
        tracker.sync(vec![], &center);
        assert_eq!(center.take(), vec!["withdraw lightbulb.request.ask-1"]);
        // Dropping the live set withdraws what is still up.
        let mut live = Live { tracker: Tracker::default(), center: Box::new(center.clone()) };
        live.tracker.sync(notices(json!([held()])), &center);
        center.take();
        drop(live);
        assert_eq!(center.take(), vec!["withdraw lightbulb.request.r1"]);
    }

    #[test]
    fn held_notice_allow_and_decline_build_the_band_s_decision_body_from_the_notification_alone() {
        let n = notices(json!([held(), ask()]));
        let (owner, device, allow) = decision_request(&n[0].info, "allow").unwrap();
        assert_eq!((owner.as_str(), device.as_str()), MAC);
        assert_eq!(allow, json!({"sessionId":SID,"generation":"g1","decision":"allow","userId":"user_aden","requestId":"r1"}));
        let (_, _, decline) = decision_request(&n[1].info, "decline").unwrap();
        assert_eq!(decline, json!({"sessionId":SID,"generation":"g1","decision":"decline","userId":"agent-friday","requestId":"ask-1"}));
        // Only Allow and Decline, and only a notification that carries the whole decision.
        assert!(decision_request(&n[0].info, "pause").is_none());
        let mut partial = n[0].info.clone();
        partial.remove("deviceId");
        assert!(decision_request(&partial, "allow").is_none());
        // A decision the backend did not take re-posts the same banner with the failure line.
        assert_eq!(failed(&n[0]), Notice { body: FAILED.into(), ..n[0].clone() });
        assert_eq!(FAILED, "Could not send. Try again.");
    }

    /// The press reaches a process that runs no local sessions (no presence, no controller,
    /// nothing posted): it still sends the right decision, signed as this Mac.
    #[test]
    fn held_notice_a_press_in_a_process_that_does_not_serve_still_sends_the_decision() {
        use wiremock::matchers::{body_json, header_exists, method, path};
        let _g = presence::TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        presence::retain(&[]); // nothing this process knows about the session
        tauri::async_runtime::block_on(async {
            let server = wiremock::MockServer::start().await;
            let expected = json!({"sessionId":SID,"generation":"g1","decision":"allow","userId":"user_aden","requestId":"r1"});
            wiremock::Mock::given(method("POST"))
                .and(path("/api/v1/messaging/native-session-decisions"))
                .and(header_exists("authorization"))
                .and(body_json(&expected))
                .respond_with(wiremock::ResponseTemplate::new(200).set_body_json(json!({})))
                .expect(1)
                .mount(&server)
                .await;
            let identity = crate::keychain::generate_keypair();
            let n = notices(json!([held()])).remove(0);
            assert_eq!(post_decision(&server.uri(), &identity, &n.info, "allow").await, Ok(()));
            let token = server.received_requests().await.unwrap()[0].headers["authorization"].to_str().unwrap().to_owned();
            assert!(token.starts_with("Bearer ") && token.len() > 20);
            // A refusal is reported, so the banner comes back saying it could not send.
            let refusing = wiremock::MockServer::start().await;
            wiremock::Mock::given(method("POST")).respond_with(wiremock::ResponseTemplate::new(409)).mount(&refusing).await;
            assert!(post_decision(&refusing.uri(), &identity, &n.info, "allow").await.is_err());
        });
    }

    #[test]
    fn held_notice_an_agent_is_told_once_per_request_and_offered_no_answer() {
        let _g = presence::TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sid = "88888888-8888-4888-8888-888888888888";
        let push = |requests: Value| presence::offer_frame(&json!({"sessionId":sid,"generation":"g1","paused":false,"viewers":[],"joinRequests":requests,"at":1}));
        push(json!([held()]));
        let line = "Lightbulb: Aden's Codex wants to message this session; your user can allow it from the Mac notification or the Lightbulb app.";
        assert_eq!(untold(sid, "g1"), vec![line.to_string()]);
        assert!(untold(sid, "g1").is_empty(), "once per request");
        push(json!([held(), ask()]));
        assert_eq!(untold(sid, "g1"), vec![notice_line("Friday")]);
        assert!(!line.contains("lightbulb ") && !line.contains('`'), "no command to run");
        presence::retain(&[]);
        assert_eq!(
            hook_output(&[line.to_string()]),
            Some(json!({"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":line}}))
        );
        assert_eq!(hook_output(&[]), None);
    }
}
