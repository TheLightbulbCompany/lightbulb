//! Who watches, who may type, pending join requests and pause, per session, as the backend last
//! said (heartbeat ack `presence`, or a `local.session.presence` frame). Read by `mod_socket`
//! for the Claude terminal's band. A pending join request freezes remote typing for that session
//! (spec 2026-10-04-session-sharing-mod §2.4): the request is stored before the mod can read it,
//! so no teammate key can press Allow.

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
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub(super) struct Entry {
    pub generation: String,
    pub paused: bool,
    pub viewers: Vec<Viewer>,
    pub join_requests: Vec<JoinRequest>,
}

static PRESENCE: Mutex<BTreeMap<String, Entry>> = Mutex::new(BTreeMap::new());

fn parse(body: &Value) -> Option<(String, Entry)> {
    let sid = body["sessionId"].as_str().filter(|s| uuid::Uuid::parse_str(s).is_ok())?.to_owned();
    let entry = serde_json::from_value(body.clone()).ok()?;
    Some((sid, entry))
}

/// A heartbeat ack's `presence`: the whole picture for this Mac. Absent (an older backend or a
/// failed read) leaves what we have.
pub(super) fn offer_ack(list: &Value) {
    let Some(list) = list.as_array() else { return };
    let fresh: BTreeMap<_, _> = list.iter().take(64).filter_map(parse).collect();
    if let Ok(mut map) = PRESENCE.lock() {
        *map = fresh;
    }
}

/// A `local.session.presence` frame: one session, now. Never answered.
pub(super) fn offer_frame(body: &Value) {
    if let Some((sid, entry)) = parse(body) {
        set(&sid, entry);
    }
}

pub(super) fn set(sid: &str, entry: Entry) {
    if let Ok(mut map) = PRESENCE.lock() {
        map.insert(sid.to_owned(), entry);
    }
}

pub(super) fn get(sid: &str) -> Option<Entry> {
    PRESENCE.lock().ok()?.get(sid).cloned()
}

/// Remote typing is dropped while the owner has a join request to answer.
pub(super) fn frozen(sid: &str) -> bool {
    get(sid).is_some_and(|e| !e.join_requests.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const SID: &str = "11111111-1111-4111-8111-111111111111";

    fn entry(requests: serde_json::Value) -> serde_json::Value {
        json!({"sessionId":SID,"generation":"g1","paused":false,
               "viewers":[{"userId":"u1","name":"Aden","canType":true,"typing":false}],
               "joinRequests":requests})
    }

    #[test]
    fn presence_frame_with_a_request_freezes_and_an_empty_one_lifts() {
        offer_frame(&entry(json!([{"userId":"u2","name":"Mira","requestId":"r1"}])));
        assert!(frozen(SID));
        assert_eq!(get(SID).unwrap().viewers[0].name, "Aden");
        offer_frame(&entry(json!([])));
        assert!(!frozen(SID));
    }

    #[test]
    fn presence_ack_replaces_every_session_and_forgets_the_unlisted() {
        let other = "22222222-2222-4222-8222-222222222222";
        offer_frame(&json!({"sessionId":other,"generation":"g1","paused":true,"viewers":[],"joinRequests":[]}));
        offer_ack(&json!([entry(json!([]))]));
        assert!(get(other).is_none());
        assert!(get(SID).is_some());
    }

    #[test]
    fn presence_ignores_a_bad_session_id_and_absent_lists() {
        offer_frame(&json!({"sessionId":"nope","generation":"g1","paused":false,"viewers":[],"joinRequests":[]}));
        assert!(get("nope").is_none());
        offer_ack(&serde_json::Value::Null); // an older backend: nothing changes, nothing panics
    }
}
