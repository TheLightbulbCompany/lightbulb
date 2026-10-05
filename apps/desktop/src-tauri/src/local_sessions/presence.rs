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

/// A heartbeat ack's `presence` for the sessions its frame sent (`sids`): a heartbeat can span
/// several frames, each with its own ack, so an ack replaces or removes only those sessions and
/// leaves every other entry alone. Absent (an older backend or a failed read) leaves what we
/// have. The ack and a frame are independent posts, so an ack read before a frame that landed
/// after `sent` must not undo it: such a session keeps the ack's entry with the union (by user)
/// of both pending requests, or the frame's entry if the ack omits it.
pub(super) fn offer_ack(list: &Value, sent: Instant, sids: &[String]) {
    let Some(list) = list.as_array() else { return };
    let mut fresh: BTreeMap<_, _> = list.iter().take(64).filter_map(parse).collect();
    let Ok(mut map) = PRESENCE.lock() else { return };
    for sid in sids {
        let late = map.get(sid).filter(|(_, at)| at.is_some_and(|t| t > sent)).cloned();
        match (fresh.remove(sid), late) {
            (Some(mut e), Some((old, at))) => {
                for r in old.join_requests {
                    if !e.join_requests.iter().any(|x| x.user_id == r.user_id) {
                        e.join_requests.push(r);
                    }
                }
                map.insert(sid.clone(), (e, at));
            }
            (Some(e), None) => {
                map.insert(sid.clone(), (e, None));
            }
            (None, Some(_)) => {}
            (None, None) => {
                map.remove(sid);
            }
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

/// A `local.session.presence` frame: one session, now. Never answered.
pub(super) fn offer_frame(body: &Value) {
    if let Some((sid, entry)) = parse(body) {
        if let Ok(mut map) = PRESENCE.lock() {
            map.insert(sid, (entry, Some(Instant::now())));
        }
    }
}

#[cfg(test)]
pub(super) fn set(sid: &str, entry: Entry) {
    if let Ok(mut map) = PRESENCE.lock() {
        map.insert(sid.to_owned(), (entry, None));
    }
}

pub(super) fn get(sid: &str) -> Option<Entry> {
    PRESENCE.lock().ok()?.get(sid).map(|(e, _)| e.clone())
}

/// Whether `sid` still holds this pending request. An Allow or Decline is taken only then: the
/// request is on the band and remote typing is frozen.
pub(super) fn pending(sid: &str, user: &str, request: &str) -> bool {
    get(sid).is_some_and(|e| e.join_requests.iter().any(|r| r.user_id == user && r.request_id == request))
}

/// A decision the backend took (`payload` as sent, `session` as answered), applied now so the
/// reply is not the pre-decision state. The backend's presence push follows and replaces it.
pub(super) fn decided(sid: &str, payload: &Value, session: &Value) {
    let Ok(mut map) = PRESENCE.lock() else { return };
    let blank = || Entry { generation: payload["generation"].as_str().unwrap_or("").to_owned(), paused: false, viewers: vec![], join_requests: vec![] };
    let (e, at) = map.entry(sid.to_owned()).or_insert_with(|| (blank(), None));
    match payload["decision"].as_str() {
        Some("allow" | "decline") => e.join_requests.retain(|r| Some(r.request_id.as_str()) != payload["requestId"].as_str()),
        _ => {
            if let Some(shared) = session["shared"].as_bool() {
                e.paused = !shared;
            }
        }
    }
    *at = Some(Instant::now());
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
        offer_ack(&json!([entry(json!([]))]), Instant::now(), &[SID.into(), other.into()]);
        assert!(get(other).is_none());
        assert!(get(SID).is_some());
    }

    #[test]
    fn presence_ignores_a_bad_session_id_and_absent_lists() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        offer_frame(&json!({"sessionId":"nope","generation":"g1","paused":false,"viewers":[],"joinRequests":[]}));
        assert!(get("nope").is_none());
        offer_ack(&serde_json::Value::Null, Instant::now(), &[SID.into()]); // an older backend: nothing changes, nothing panics
    }

    fn req(u: &str) -> serde_json::Value {
        json!({"userId":u,"name":u,"requestId":format!("r-{u}")})
    }

    #[test]
    fn presence_a_frame_after_the_heartbeat_survives_a_stale_ack() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sent = Instant::now();
        offer_frame(&entry(json!([req("u2")])));
        offer_ack(&json!([entry(json!([]))]), sent, &[SID.into()]);
        assert!(frozen(SID));
        offer_ack(&json!([]), sent, &[SID.into()]); // the ack does not list the session: the frame's entry stays
        assert!(frozen(SID));
    }

    #[test]
    fn presence_a_frame_from_before_the_heartbeat_is_replaced_by_the_ack() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        offer_frame(&entry(json!([req("u2")])));
        assert!(frozen(SID));
        offer_ack(&json!([entry(json!([]))]), Instant::now(), &[SID.into()]);
        assert!(!frozen(SID));
    }

    #[test]
    fn presence_a_late_frame_and_an_ack_union_their_requests() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sent = Instant::now();
        offer_frame(&entry(json!([req("u2"), req("u3")])));
        offer_ack(&json!([entry(json!([req("u3"), req("u4")]))]), sent, &[SID.into()]);
        let ids: Vec<_> = get(SID).unwrap().join_requests.into_iter().map(|r| r.user_id).collect();
        assert_eq!(ids, ["u3", "u4", "u2"]);
    }

    #[test]
    fn presence_acks_of_a_split_heartbeat_each_touch_only_their_own_sessions() {
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let other = "33333333-3333-4333-8333-333333333333";
        let sent = Instant::now();
        let mut b = entry(json!([req("u5")]));
        b["sessionId"] = json!(other);
        offer_ack(&json!([entry(json!([req("u2")]))]), sent, &[SID.into()]);
        offer_ack(&json!([b]), sent, &[other.into()]);
        assert!(frozen(SID) && frozen(other), "the second frame's ack must not erase the first's");
        offer_ack(&json!([]), Instant::now(), &[other.into()]);
        assert!(frozen(SID) && !frozen(other));
        retain(&[]);
        assert!(get(SID).is_none());
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
        offer_frame(&entry(json!([req("u2"), req("u3")])));
        assert!(pending(SID, "u2", "r-u2") && !pending(SID, "u2", "r-other"));
        decided(SID, &json!({"decision":"allow","userId":"u2","requestId":"r-u2"}), &json!({"shared":true}));
        assert!(!pending(SID, "u2", "r-u2") && pending(SID, "u3", "r-u3"));
        decided(SID, &json!({"decision":"pause"}), &json!({"shared":false}));
        assert!(get(SID).unwrap().paused);
        decided(SID, &json!({"decision":"unpause"}), &json!({"shared":true}));
        assert!(!get(SID).unwrap().paused);
    }
}
