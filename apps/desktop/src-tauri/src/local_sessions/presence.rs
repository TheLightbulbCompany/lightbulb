//! Who watches, who may type, pending join requests and pause, per session, as the backend last
//! said (heartbeat ack `presence`, or a `local.session.presence` frame). Read by `mod_socket`
//! for the Claude terminal's band. A pending join request freezes remote typing for that session
//! (spec 2026-10-04-session-sharing-mod §2.4): the request is stored before the mod can read it,
//! so no teammate key can press Allow.

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::Instant;

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

/// Each entry with the instant a frame set it (None when the ack did).
static PRESENCE: Mutex<BTreeMap<String, (Entry, Option<Instant>)>> = Mutex::new(BTreeMap::new());

/// Tests share the one global map; each takes this first.
#[cfg(test)]
pub(super) static TEST_LOCK: Mutex<()> = Mutex::new(());

fn parse(body: &Value) -> Option<(String, Entry)> {
    let sid = body["sessionId"].as_str().filter(|s| uuid::Uuid::parse_str(s).is_ok())?.to_owned();
    let entry = serde_json::from_value(body.clone()).ok()?;
    Some((sid, entry))
}

/// A heartbeat ack's `presence`: the whole picture for this Mac. Absent (an older backend or a
/// failed read) leaves what we have. The ack and a frame are independent posts, so an ack read
/// before a frame that landed after `sent` must not undo it: such a session keeps the ack's entry
/// with the union (by user) of both pending requests, or the frame's entry if the ack omits it.
pub(super) fn offer_ack(list: &Value, sent: Instant) {
    let Some(list) = list.as_array() else { return };
    let mut fresh: BTreeMap<_, _> = list.iter().take(64).filter_map(parse).map(|(k, e)| (k, (e, None))).collect();
    if let Ok(mut map) = PRESENCE.lock() {
        for (sid, (old, at)) in map.iter() {
            if !at.is_some_and(|t| t > sent) {
                continue;
            }
            match fresh.get_mut(sid) {
                None => {
                    fresh.insert(sid.clone(), (old.clone(), *at));
                }
                Some((e, kept)) => {
                    for r in &old.join_requests {
                        if !e.join_requests.iter().any(|x| x.user_id == r.user_id) {
                            e.join_requests.push(r.clone());
                        }
                    }
                    *kept = *at;
                }
            }
        }
        *map = fresh;
    }
}

/// A `local.session.presence` frame: one session, now. Never answered.
pub(super) fn offer_frame(body: &Value) {
    if let Some((sid, entry)) = parse(body) {
        if let Ok(mut map) = PRESENCE.lock() {
            map.insert(sid, (entry, Some(Instant::now())));
        }
    }
}

pub(super) fn set(sid: &str, entry: Entry) {
    if let Ok(mut map) = PRESENCE.lock() {
        map.insert(sid.to_owned(), (entry, None));
    }
}

pub(super) fn get(sid: &str) -> Option<Entry> {
    PRESENCE.lock().ok()?.get(sid).map(|(e, _)| e.clone())
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
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        offer_frame(&entry(json!([{"userId":"u2","name":"Mira","requestId":"r1"}])));
        assert!(frozen(SID));
        assert_eq!(get(SID).unwrap().viewers[0].name, "Aden");
        offer_frame(&entry(json!([])));
        assert!(!frozen(SID));
    }

    #[test]
    fn presence_ack_replaces_every_session_and_forgets_the_unlisted() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let other = "22222222-2222-4222-8222-222222222222";
        offer_frame(&json!({"sessionId":other,"generation":"g1","paused":true,"viewers":[],"joinRequests":[]}));
        offer_ack(&json!([entry(json!([]))]), Instant::now());
        assert!(get(other).is_none());
        assert!(get(SID).is_some());
    }

    #[test]
    fn presence_ignores_a_bad_session_id_and_absent_lists() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        offer_frame(&json!({"sessionId":"nope","generation":"g1","paused":false,"viewers":[],"joinRequests":[]}));
        assert!(get("nope").is_none());
        offer_ack(&serde_json::Value::Null, Instant::now()); // an older backend: nothing changes, nothing panics
    }

    fn req(u: &str) -> serde_json::Value {
        json!({"userId":u,"name":u,"requestId":format!("r-{u}")})
    }

    #[test]
    fn presence_a_frame_after_the_heartbeat_survives_a_stale_ack() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sent = Instant::now();
        offer_frame(&entry(json!([req("u2")])));
        offer_ack(&json!([entry(json!([]))]), sent);
        assert!(frozen(SID));
        offer_ack(&json!([]), sent); // the ack does not list the session: the frame's entry stays
        assert!(frozen(SID));
    }

    #[test]
    fn presence_a_frame_from_before_the_heartbeat_is_replaced_by_the_ack() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        offer_frame(&entry(json!([req("u2")])));
        assert!(frozen(SID));
        offer_ack(&json!([entry(json!([]))]), Instant::now());
        assert!(!frozen(SID));
    }

    #[test]
    fn presence_a_late_frame_and_an_ack_union_their_requests() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sent = Instant::now();
        offer_frame(&entry(json!([req("u2"), req("u3")])));
        offer_ack(&json!([entry(json!([req("u3"), req("u4")]))]), sent);
        let ids: Vec<_> = get(SID).unwrap().join_requests.into_iter().map(|r| r.user_id).collect();
        assert_eq!(ids, ["u3", "u4", "u2"]);
    }
}
