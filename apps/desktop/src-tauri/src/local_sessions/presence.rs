//! Who watches, who may type, pending join requests and pause, per session, as the backend last
//! said (heartbeat ack `presence`, or a `local.session.presence` frame). Read by `mod_socket`
//! for the Claude terminal's band. Only the owner can operate the band: remote input never
//! carries ctrl+x or mouse reports (`remote_input`, spec 2026-10-04-session-sharing-mod §2.4).

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::Mutex;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub(super) struct Viewer {
    pub user_id: String,
    pub name: String,
    pub can_type: bool,
    pub typing: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub(super) struct JoinRequest {
    pub user_id: String,
    pub name: String,
    pub request_id: String,
    /// "join" (a person asking in), "pickup" (asking to take the session over) or "agent_message"
    /// (an agent asking to message the session; `user_id` is the agent's id); an older backend sends none.
    #[serde(default = "join_kind")]
    pub kind: String,
    /// An agent's ask only: "send" or "interrupt", and the message, clipped by the backend.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub action: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    /// A held session send's sender as delivery names it ("Aden's Codex"); a join with `text`
    /// is one with a held send (Task 7b).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
}

fn join_kind() -> String {
    "join".into()
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub(super) struct Entry {
    pub generation: String,
    pub paused: bool,
    pub viewers: Vec<Viewer>,
    pub join_requests: Vec<JoinRequest>,
    /// How many people watch on the session's "Anyone with the link" page: a count, never who
    /// (0 from an older backend, and whenever the link is not public).
    #[serde(default)]
    pub link_viewers: u32,
    /// Server milliseconds when the backend read the session row (0 from an older backend). The
    /// ack and the push are independent posts that can arrive in either order: the newest read
    /// wins, never the latest arrival.
    #[serde(default)]
    pub at: u64,
}

static PRESENCE: Mutex<BTreeMap<String, Entry>> = Mutex::new(BTreeMap::new());


/// Tests share the one global map; each takes this first.
#[cfg(test)]
pub(super) static TEST_LOCK: Mutex<()> = Mutex::new(());

fn parse(body: &Value) -> Option<(String, Entry)> {
    let sid = body["sessionId"].as_str().filter(|s| uuid::Uuid::parse_str(s).is_ok())?.to_owned();
    let entry = serde_json::from_value(body.clone()).ok()?;
    Some((sid, entry))
}

/// Stores `entry` unless the one held was read later. An equal `at` replaces.
fn offer(map: &mut BTreeMap<String, Entry>, sid: String, entry: Entry) {
    if map.get(&sid).is_none_or(|held| entry.at >= held.at) {
        map.insert(sid, entry);
    }
}

/// A heartbeat ack (`presence`, read at `presenceAt`) for the sessions its frame sent (`sids`): a
/// heartbeat can span several frames, each with its own ack, so an ack replaces or removes only
/// those sessions and leaves every other entry alone. A session the ack leaves out is removed
/// only when what we hold was read no later than the ack. Absent `presence` (an older backend or
/// a failed read) leaves what we have.
pub(super) fn offer_ack(ack: &Value, sids: &[String]) {
    let Some(list) = ack["presence"].as_array() else { return };
    let read_at = ack["presenceAt"].as_u64().unwrap_or(0);
    let mut fresh: BTreeMap<_, _> = list.iter().take(64).filter_map(parse).collect();
    let Ok(mut map) = PRESENCE.lock() else { return };
    for sid in sids {
        match fresh.remove(sid) {
            Some(e) => offer(&mut map, sid.clone(), e),
            None if map.get(sid).is_some_and(|held| held.at <= read_at) => {
                map.remove(sid);
            }
            None => {}
        }
    }
}

/// A new heartbeat round names every session this Mac still reports: forget the rest.
pub(super) fn retain(sids: &[String]) {
    if let Ok(mut map) = PRESENCE.lock() {
        map.retain(|sid, _| sids.contains(sid));
    }
}

/// The session ids one heartbeat frame carries.
pub(super) fn frame_sids(frame: &Value) -> Vec<String> {
    frame["sessions"].as_array().into_iter().flatten().filter_map(|s| s["sessionId"].as_str().map(str::to_owned)).collect()
}

/// A `local.session.presence` frame: one session. Never answered.
pub(super) fn offer_frame(body: &Value) {
    if let Some((sid, entry)) = parse(body) {
        if let Ok(mut map) = PRESENCE.lock() {
            offer(&mut map, sid, entry);
        }
    }
}

#[cfg(test)]
pub(super) fn set(sid: &str, entry: Entry) {
    if let Ok(mut map) = PRESENCE.lock() {
        map.insert(sid.to_owned(), entry);
    }
}

/// The entry for `sid` only while it is of `generation`: a session restarted under the same id
/// must not inherit the old run's viewers or pending requests.
pub(super) fn get(sid: &str, generation: &str) -> Option<Entry> {
    PRESENCE.lock().ok()?.get(sid).filter(|e| e.generation == generation).cloned()
}

/// Whether `sid` still holds this pending request. An Allow or Decline is taken only then.
pub(super) fn pending(sid: &str, generation: &str, user: &str, request: &str) -> bool {
    get(sid, generation).is_some_and(|e| e.join_requests.iter().any(|r| r.user_id == user && r.request_id == request))
}

/// A decision the backend took (`payload` as sent, `session` as answered), applied now so the
/// reply is not the pre-decision state. It keeps the held `at`, so the backend's presence push
/// that follows (read later) replaces it.
pub(super) fn decided(sid: &str, payload: &Value, session: &Value) {
    let Ok(mut map) = PRESENCE.lock() else { return };
    let generation = payload["generation"].as_str().unwrap_or("");
    let e = map.entry(sid.to_owned()).or_insert_with(|| Entry { generation: generation.to_owned(), paused: false, viewers: vec![], join_requests: vec![], link_viewers: 0, at: 0 });
    if e.generation != generation {
        *e = Entry { generation: generation.to_owned(), paused: false, viewers: vec![], join_requests: vec![], link_viewers: 0, at: e.at };
    }
    match payload["decision"].as_str() {
        Some("allow" | "decline") => e.join_requests.retain(|r| Some(r.request_id.as_str()) != payload["requestId"].as_str()),
        _ => {
            if let Some(shared) = session["shared"].as_bool() {
                e.paused = !shared;
            }
        }
    }
    drop(map);
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const SID: &str = "11111111-1111-4111-8111-111111111111";

    fn entry_at(generation: &str, at: u64, requests: serde_json::Value) -> serde_json::Value {
        json!({"sessionId":SID,"generation":generation,"paused":false,"at":at,
               "viewers":[{"userId":"u1","name":"Aden","canType":true,"typing":false}],
               "joinRequests":requests})
    }

    fn entry(requests: serde_json::Value) -> serde_json::Value {
        entry_at("g1", 100, requests)
    }

    fn req(u: &str) -> serde_json::Value {
        json!({"userId":u,"name":u,"requestId":format!("r-{u}")})
    }

    fn ack(list: serde_json::Value, read_at: u64) -> serde_json::Value {
        json!({"presence":list,"presenceAt":read_at})
    }

    fn reset() {
        retain(&[]);
    }

    /// Whether `sid` holds any pending request at `generation`.
    fn asking(sid: &str, generation: &str) -> bool {
        get(sid, generation).is_some_and(|e| !e.join_requests.is_empty())
    }

    #[test]
    fn presence_frame_with_a_request_stores_it_and_an_empty_one_clears_it() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        offer_frame(&entry(json!([req("u2")])));
        assert!(asking(SID, "g1"));
        assert_eq!(get(SID, "g1").unwrap().viewers[0].name, "Aden");
        offer_frame(&entry(json!([])));
        assert!(!asking(SID, "g1"));
    }

    #[test]
    fn presence_ack_replaces_its_sessions_and_removes_the_ones_it_leaves_out() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        let other = "22222222-2222-4222-8222-222222222222";
        offer_frame(&json!({"sessionId":other,"generation":"g1","paused":true,"viewers":[],"joinRequests":[],"at":100}));
        offer_ack(&ack(json!([entry(json!([]))]), 200), &[SID.into(), other.into()]);
        assert!(get(other, "g1").is_none());
        assert!(get(SID, "g1").is_some());
    }

    #[test]
    fn presence_ignores_a_bad_session_id_and_absent_lists() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        offer_frame(&json!({"sessionId":"nope","generation":"g1","paused":false,"viewers":[],"joinRequests":[]}));
        assert!(get("nope", "g1").is_none());
        offer_frame(&entry(json!([req("u2")])));
        offer_ack(&json!({"presenceAt": 900}), &[SID.into()]); // no `presence`: nothing changes, nothing panics
        assert!(asking(SID, "g1"));
    }

    #[test]
    fn presence_an_ack_read_before_a_frame_neither_replaces_nor_removes_it() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        offer_frame(&entry_at("g1", 300, json!([req("u2")])));
        offer_ack(&ack(json!([entry_at("g1", 200, json!([]))]), 200), &[SID.into()]);
        assert!(asking(SID, "g1"), "an older ack entry is ignored");
        offer_ack(&ack(json!([]), 200), &[SID.into()]);
        assert!(asking(SID, "g1"), "an older ack that leaves the session out does not remove it");
    }

    #[test]
    fn presence_an_ack_with_a_higher_at_replaces_a_frame() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        offer_frame(&entry_at("g1", 100, json!([req("u2")])));
        offer_ack(&ack(json!([entry_at("g1", 200, json!([]))]), 200), &[SID.into()]);
        assert!(!asking(SID, "g1"));
        offer_frame(&entry_at("g1", 300, json!([req("u2")])));
        offer_ack(&ack(json!([]), 400), &[SID.into()]);
        assert!(get(SID, "g1").is_none(), "a later ack that leaves the session out removes it");
    }

    #[test]
    fn presence_a_delayed_frame_does_not_replace_a_newer_request() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        let ask = |rid: &str| json!([{"userId":"u2","name":"u2","requestId":rid}]);
        offer_frame(&entry_at("g1", 200, ask("r2")));
        offer_frame(&entry_at("g1", 100, ask("r1"))); // the push for r1, delivered late
        assert!(pending(SID, "g1", "u2", "r2") && !pending(SID, "g1", "u2", "r1"));
        offer_frame(&entry_at("g1", 200, json!([])));
        assert!(!asking(SID, "g1"), "an equal `at` replaces");
    }

    #[test]
    fn presence_an_old_generation_frame_does_not_clear_the_current_one() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        offer_frame(&entry_at("g2", 200, json!([req("u2")])));
        offer_frame(&entry_at("g1", 100, json!([]))); // the old run's push, delivered late
        assert!(asking(SID, "g2") && pending(SID, "g2", "u2", "r-u2"));
        // The read-side check stays: g2's request never answers for another generation.
        assert!(!asking(SID, "g1") && !pending(SID, "g3", "u2", "r-u2") && get(SID, "g1").is_none());
        // A g3 decision on a stored g2 entry starts a clean g3 entry.
        decided(SID, &json!({"decision":"pause","generation":"g3"}), &json!({"shared":false}));
        let e = get(SID, "g3").unwrap();
        assert!(e.paused && e.join_requests.is_empty());
    }

    #[test]
    fn presence_request_kind_round_trips_and_defaults_to_join() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        let pickup = json!([{"userId":"u2","name":"Mira","requestId":"r1","kind":"pickup"}]);
        offer_frame(&entry_at("g1", 100, pickup));
        assert_eq!(get(SID, "g1").unwrap().join_requests[0].kind, "pickup");
        assert!(asking(SID, "g1"));
        offer_frame(&entry_at("g1", 200, json!([req("u3")])));
        assert_eq!(get(SID, "g1").unwrap().join_requests[0].kind, "join");
    }

    #[test]
    fn presence_an_agent_ask_parses_and_allow_answers_it_with_the_agent_id() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        let ask = json!([{"userId":"agent-friday","name":"Friday","requestId":"ask-1","kind":"agent_message","action":"send","text":"Run the tests"}]);
        offer_frame(&entry_at("g1", 100, ask));
        let r = &get(SID, "g1").unwrap().join_requests[0];
        assert_eq!((r.kind.as_str(), r.action.as_deref(), r.text.as_deref()), ("agent_message", Some("send"), Some("Run the tests")));
        let allow = super::super::mod_socket::decision_body(SID, "g1", &json!({"decision":"allow","userId":r.user_id,"requestId":r.request_id})).unwrap();
        assert_eq!((allow["userId"].as_str(), allow["requestId"].as_str()), (Some("agent-friday"), Some("ask-1")));
        assert!(!super::super::mod_socket::stale(SID, &allow));
    }

    #[test]
    fn presence_an_entry_without_at_never_beats_one_with_at() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        offer_frame(&entry_at("g1", 1, json!([req("u2")])));
        let mut old = entry(json!([]));
        old.as_object_mut().unwrap().remove("at");
        offer_frame(&old);
        offer_ack(&json!({"presence":[old]}), &[SID.into()]);
        offer_ack(&json!({"presence":[]}), &[SID.into()]);
        assert!(asking(SID, "g1"));
    }

    #[test]
    fn presence_acks_of_a_split_heartbeat_each_touch_only_their_own_sessions() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        let other = "33333333-3333-4333-8333-333333333333";
        let mut b = entry(json!([req("u5")]));
        b["sessionId"] = json!(other);
        offer_ack(&ack(json!([entry(json!([req("u2")]))]), 100), &[SID.into()]);
        offer_ack(&ack(json!([b]), 100), &[other.into()]);
        assert!(asking(SID, "g1") && asking(other, "g1"), "the second frame's ack must not erase the first's");
        offer_ack(&ack(json!([]), 500), &[other.into()]);
        assert!(asking(SID, "g1") && !asking(other, "g1"));
        retain(&[]);
        assert!(get(SID, "g1").is_none());
    }

    #[test]
    fn presence_frame_sids_reads_the_heartbeat_sessions() {
        let frame = json!({"type":"local.device.heartbeat","sessions":[{"sessionId":"a"},{"sessionId":"b"}]});
        assert_eq!(frame_sids(&frame), ["a", "b"]);
        assert!(frame_sids(&json!({"sessions":[]})).is_empty());
    }

    #[test]
    fn presence_a_decision_drops_the_answered_request_and_sets_pause() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        reset();
        offer_frame(&entry(json!([req("u2"), req("u3")])));
        assert!(pending(SID, "g1", "u2", "r-u2") && !pending(SID, "g1", "u2", "r-other"));
        decided(SID, &json!({"decision":"allow","generation":"g1","userId":"u2","requestId":"r-u2"}), &json!({"shared":true}));
        assert!(!pending(SID, "g1", "u2", "r-u2") && pending(SID, "g1", "u3", "r-u3"));
        decided(SID, &json!({"decision":"pause","generation":"g1"}), &json!({"shared":false}));
        assert!(get(SID, "g1").unwrap().paused);
        decided(SID, &json!({"decision":"unpause","generation":"g1"}), &json!({"shared":true}));
        assert!(!get(SID, "g1").unwrap().paused);
        offer_frame(&entry(json!([req("u3")]))); // the backend's push, read at the same `at`, replaces it
        assert!(pending(SID, "g1", "u3", "r-u3") && !pending(SID, "g1", "u2", "r-u2"));
    }
}
