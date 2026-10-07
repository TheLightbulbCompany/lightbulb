//! Session states beyond a running terminal (spec 2026-10-07 §5.1, §5.4): an ended held session's
//! final screen, which its holder keeps while this app runs, and why a running session has no
//! terminal. `transport.rs` calls in here at eight one-line seams; `screen.rs` is untouched: a kept
//! ended screen is indexed as live, so a viewer's watch reaches its holder like a running one's.
//!
//! A screen is an ended session's only when its holder's provider exited and that session ended
//! with it. `/clear`, `/resume`, `/new` and the agent view end a conversation while its holder runs
//! on with another: that terminal is never the ended session's screen.

use super::{Mirror, NativeSession};
use crate::holder::manager::{self, HolderInfo};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, Ordering::SeqCst};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// The relay lists `endedScreens`: it reads `screens` heartbeats and `screen` on an end.
static READS_ENDED_SCREENS: AtomicBool = AtomicBool::new(false);
/// The relay lists `terminalReason`.
static READS_TERMINAL_REASON: AtomicBool = AtomicBool::new(false);
/// `DeviceHeartbeat.screens`' cap on the relay.
const SCREENS_MAX: usize = 24;
/// How often holders no mirror names are looked for.
const SWEEP_EVERY: Duration = Duration::from_secs(10);
static SWEPT: Mutex<Option<Instant>> = Mutex::new(None);
/// A session that ended up to this long before its holder's provider exited ended with it: the
/// `SessionEnd` hook comes first, then the provider shuts down.
// ponytail: a time window, not an identity (a holder does not know its conversation). A conversation
// left less than this before the provider quit either ties with the one that replaced it (no screen
// is kept) or, with none after it (the agent view), keeps those last seconds' screen.
const ENDED_WITH_MS: u64 = 10_000;
/// Holder id -> when its provider exited (None: it still ran when last asked, at that Instant).
static ENDS: Mutex<BTreeMap<String, (Option<u64>, Instant)>> = Mutex::new(BTreeMap::new());
/// The mirrors whose screen the last `index` kept.
static SHOWN: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());

/// (`endedScreens`, `terminalReason`) as a registration ack's `features` lists them.
fn features(features: &Value) -> (bool, bool) {
    let has = |name: &str| features.as_array().is_some_and(|f| f.iter().any(|x| x == name));
    (has("endedScreens"), has("terminalReason"))
}

/// A registration ack's `features`. Per connection, like the other `relay_reads_*` flags:
/// `Host::reset_relay_features` clears them (Null) when a socket starts, so a reconnect to an
/// older relay sends nothing it would refuse before its own ack is read.
pub(super) fn read_features(listed: &Value) {
    let (screens, reason) = features(listed);
    READS_ENDED_SCREENS.store(screens, SeqCst);
    READS_TERMINAL_REASON.store(reason, SeqCst);
}

#[cfg(test)]
pub(super) fn reads_ended_screens() -> bool {
    READS_ENDED_SCREENS.load(SeqCst)
}

/// When this holder's provider exited (unix ms), if the holder shows only its final screen now.
/// An end never changes, so it is asked once; a holder that still runs is asked again a second later.
fn holder_end(id: &str) -> Option<u64> {
    let socket = manager::socket_dir().join(format!("{id}.sock"));
    let mut ends = ENDS.lock().ok()?;
    if UnixStream::connect(&socket).is_err() {
        ends.remove(id); // closed: its screen is gone
        return None;
    }
    match ends.get(id) {
        Some((Some(at), _)) => return Some(*at),
        Some((None, asked)) if asked.elapsed() < Duration::from_secs(1) => return None,
        _ => {}
    }
    let at = manager::ended_at(&socket);
    ends.insert(id.to_owned(), (at, Instant::now()));
    at
}

/// The ended sessions whose final screen this Mac still shows, by mirror key, newest end first, one
/// frame's worth: registered, and the only session that ended with its holder's provider. `end`:
/// when a holder's provider exited (None while it runs, or once the holder is closed).
fn kept<'a>(mirrors: &'a BTreeMap<String, Mirror>, end: &impl Fn(&str) -> Option<u64>) -> Vec<(&'a str, &'a Mirror)> {
    let mut by_holder: BTreeMap<&str, Vec<(&str, &Mirror)>> = BTreeMap::new();
    for (key, m) in mirrors {
        let (Some(at), Some(holder)) = (m.ended_at, m.native.holder.as_deref()) else { continue };
        if end(holder).is_some_and(|exited| m.ended_at_ms.unwrap_or(at * 1000) + ENDED_WITH_MS >= exited) {
            by_holder.entry(holder).or_default().push((key, m));
        }
    }
    let mut kept: Vec<(&str, &Mirror)> =
        by_holder.into_values().filter(|with| with.len() == 1).map(|with| with[0]).filter(|(_, m)| m.session_id.is_some()).collect();
    kept.sort_by_key(|(_, m)| std::cmp::Reverse(m.ended_at));
    kept.truncate(SCREENS_MAX);
    kept
}

/// A heartbeat ack's `features`, when it lists any: None from a relay older than the key, which
/// leaves what this connection knows (nothing, after `reset_relay_features`) as it is.
fn ack_features(ack: &Value) -> Option<(bool, bool)> {
    ack.get("features").map(features)
}

/// A heartbeat ack. With every session ended nothing registers after a reconnect, so this is where
/// the connection learns `endedScreens` again and its kept screens go on being renewed.
pub(super) fn read_ack(ack: &Value) {
    if let Some((screens, reason)) = ack_features(ack) {
        READS_ENDED_SCREENS.store(screens, SeqCst);
        READS_TERMINAL_REASON.store(reason, SeqCst);
    }
}

fn screens_frame(sids: &[String]) -> Value {
    json!({"type": "local.device.heartbeat", "sessions": [], "screens": sids})
}

/// The heartbeat's frames, plus one naming the kept ended screens for a relay that reads it.
pub(super) fn with_screens(frames: Vec<Value>, mirrors: &BTreeMap<String, Mirror>) -> Vec<Value> {
    with_screens_if(READS_ENDED_SCREENS.load(SeqCst), frames, mirrors, &holder_end)
}

fn with_screens_if(reads: bool, mut frames: Vec<Value>, mirrors: &BTreeMap<String, Mirror>, end: &impl Fn(&str) -> Option<u64>) -> Vec<Value> {
    let sids: Vec<String> = kept(mirrors, end).into_iter().filter_map(|(_, m)| m.session_id.clone()).collect();
    if sids.is_empty() {
        return frames;
    }
    if reads {
        frames.push(screens_frame(&sids));
    } else if frames.is_empty() {
        // Not known yet on this connection, and no session's heartbeat is going: the plain idle
        // heartbeat any relay takes, so its ack says (`read_ack`). An older relay gets one each
        // cadence while a screen is kept, and nothing else.
        frames.push(json!({"type": "local.device.heartbeat", "sessions": []}));
    }
    frames
}

/// `local.session.ended` says the final screen stays, when the holder already has it (an end the
/// hook reported before the provider exited does not: the next heartbeat's `screens` names it).
pub(super) fn add_screen(frame: &mut Value, mirrors: &BTreeMap<String, Mirror>, key: &str) {
    add_screen_if(READS_ENDED_SCREENS.load(SeqCst), frame, mirrors, key, &holder_end);
}

fn add_screen_if(reads: bool, frame: &mut Value, mirrors: &BTreeMap<String, Mirror>, key: &str, end: &impl Fn(&str) -> Option<u64>) {
    if reads && kept(mirrors, end).iter().any(|(k, _)| *k == key) {
        frame["screen"] = json!(true);
    }
}

/// A heartbeat entry's `terminalReason`, for a relay that reads it.
pub(super) fn add_reason(entry: &mut Value, native: &NativeSession) {
    add_reason_if(READS_TERMINAL_REASON.load(SeqCst), entry, native);
}

fn add_reason_if(reads: bool, entry: &mut Value, native: &NativeSession) {
    if let Some(reason) = native.terminal_reason().filter(|_| reads) {
        entry["terminalReason"] = json!(reason);
    }
}

/// `screen::index` with every kept ended screen indexed as live; then ended holders that show no
/// session's screen are closed. Returns the mirrors whose screen is newly kept: the caller brings
/// the heartbeat forward so the relay hears of it now.
pub(super) fn index(mirrors: &BTreeMap<String, Mirror>) -> Vec<String> {
    let kept = kept(mirrors, &holder_end);
    let shown = with_kept(mirrors, &holder_end);
    super::screen::index(shown.as_ref().unwrap_or(mirrors));
    sweep(named(mirrors, &kept));
    let now = kept.iter().map(|(key, _)| key.to_string()).collect();
    SHOWN.lock().map(|mut shown| fresh(&mut shown, now)).unwrap_or_default()
}

/// What is in `now` and was not in `shown`; `shown` becomes `now`.
fn fresh(shown: &mut BTreeSet<String>, now: BTreeSet<String>) -> Vec<String> {
    let new = now.difference(shown).cloned().collect();
    *shown = now;
    new
}

/// The mirrors the screen index should see; None when no ended screen is kept (nothing to copy).
// ponytail: clones the live mirrors once a second while a screen is kept; an index that took ids would not (screen.rs is S2's).
fn with_kept(mirrors: &BTreeMap<String, Mirror>, end: &impl Fn(&str) -> Option<u64>) -> Option<BTreeMap<String, Mirror>> {
    let kept: BTreeSet<&str> = kept(mirrors, end).into_iter().map(|(key, _)| key).collect();
    if kept.is_empty() {
        return None;
    }
    Some(
        mirrors
            .iter()
            .filter(|(key, m)| m.ended_at.is_none() || kept.contains(key.as_str()))
            .map(|(k, m)| (k.clone(), Mirror { ended_at: None, ..m.clone() }))
            .collect(),
    )
}

/// The holders a session still needs: a running session's, and each kept screen's.
fn named(mirrors: &BTreeMap<String, Mirror>, kept: &[(&str, &Mirror)]) -> BTreeSet<String> {
    mirrors.values().filter(|m| m.ended_at.is_none()).chain(kept.iter().map(|(_, m)| *m)).filter_map(|m| m.native.holder.clone()).collect()
}

/// Off the relay loop, at most every SWEEP_EVERY: `list` and `is_ended` talk to every holder.
fn sweep(named: BTreeSet<String>) {
    if cfg!(test) {
        return; // never signal the developer's own holders
    }
    let Ok(mut last) = SWEPT.lock() else { return };
    if last.is_some_and(|at| at.elapsed() < SWEEP_EVERY) {
        return;
    }
    *last = Some(Instant::now());
    std::thread::spawn(move || {
        for pid in orphans(&manager::list(), &named, manager::is_ended) {
            manager::stop(pid);
        }
    });
}

/// Ended holders no session needs: a resumed session's old holder once discovery names the new
/// one, a session reaped after a day, a screen past the newest SCREENS_MAX, or a final screen
/// that is no session's to show. A running holder nobody names yet (just started) never is.
fn orphans(holders: &[HolderInfo], named: &BTreeSet<String>, ended: impl Fn(&HolderInfo) -> bool) -> Vec<u32> {
    holders.iter().filter(|h| !named.contains(&h.id) && ended(h)).map(|h| h.pid).collect()
}

#[cfg(test)]
mod tests {
    use super::super::transport::tests::test_mirror;
    use super::*;

    const SID: &str = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";
    const GOLDEN: &str =
        include_str!("../../../../../packages/shared/fixtures/local-session-frames/device-heartbeat-screens.json");

    /// A registered session `sid` that ended at `at` seconds, last seen in `holder`.
    fn ended(holder: &str, at: u64, sid: &str) -> Mirror {
        let mut m = test_mirror(&format!("thread-{sid}"), "gen-1");
        m.session_id = Some(sid.into());
        m.native.holder = Some(holder.into());
        m.ended_at = Some(at);
        m.ended_at_ms = Some(at * 1000);
        m
    }

    fn keys(kept: &[(&str, &Mirror)]) -> Vec<String> {
        kept.iter().map(|(k, _)| k.to_string()).collect()
    }

    #[test]
    fn only_the_session_that_ended_with_its_holder_keeps_a_screen_and_is_indexed_as_live() {
        let mut live = test_mirror("thread-live", "gen-1");
        live.session_id = Some("live".into());
        let mut unregistered = ended("h6", 100, "x");
        unregistered.session_id = None;
        let mut alone = ended("h7", 100, "y");
        alone.session_id = None;
        let mirrors = BTreeMap::from([
            ("a".to_string(), ended("h1", 100, SID)),
            // `/clear`, `/resume` or the agent view: the conversation ended, its holder runs on.
            ("b".to_string(), ended("running", 100, "cleared")),
            // It left this holder long before the provider exited: that final screen is not its own.
            ("c".to_string(), ended("h3", 50, "left-earlier")),
            // Two ended with one holder: nothing says whose screen it is.
            ("d".to_string(), ended("h4", 99, "one")),
            ("e".to_string(), ended("h4", 100, "other")),
            // An unregistered session counts as the other candidate, and keeps nothing itself.
            ("f".to_string(), ended("h6", 100, "registered")),
            ("g".to_string(), unregistered),
            ("h".to_string(), alone),
            ("i".to_string(), ended("gone", 100, "closed")),
            ("j".to_string(), live),
        ]);
        // Each holder's provider exited at 101 s; `running` still runs and `gone` does not answer.
        let end = |id: &str| (id != "running" && id != "gone").then_some(101_000);
        assert_eq!(keys(&kept(&mirrors, &end)), ["a"]);
        let shown = with_kept(&mirrors, &end).unwrap();
        assert_eq!(shown.keys().cloned().collect::<Vec<_>>(), ["a", "j"]);
        assert!(shown.values().all(|m| m.ended_at.is_none()));
        assert!(with_kept(&mirrors, &|_: &str| None).is_none(), "nothing kept: the mirrors go in as they are");
        // The hook's end comes first, then the provider shuts down: still the same end.
        let slow = BTreeMap::from([("a".to_string(), ended("h1", 100, SID))]);
        assert_eq!(keys(&kept(&slow, &|_: &str| Some(100_000 + ENDED_WITH_MS))), ["a"]);
        assert!(kept(&slow, &|_: &str| Some(100_001 + ENDED_WITH_MS)).is_empty());
        // The process was seen gone after the holder ended.
        assert_eq!(keys(&kept(&slow, &|_: &str| Some(1))), ["a"]);
    }

    #[test]
    fn screens_are_newest_first_and_one_frame_at_most() {
        let mirrors: BTreeMap<String, Mirror> =
            (0..30u64).map(|i| (format!("k{i}"), ended(&format!("{i:08}"), i, &format!("s{i}")))).collect();
        let kept = kept(&mirrors, &|_: &str| Some(0));
        assert_eq!(kept.len(), 24);
        assert_eq!(kept[0].1.session_id.as_deref(), Some("s29"));
    }

    #[test]
    fn a_reconnect_with_only_ended_screens_learns_the_feature_again_from_a_heartbeat_ack() {
        let mirrors = BTreeMap::from([("a".to_string(), ended("ab12cd34", 5, SID))]);
        let end = |_: &str| Some(5_000);
        // A new socket: nothing is known, and no live session will register. The heartbeat is
        // the plain idle one any relay takes, sent so that its ack can say what the relay reads.
        let (reads, _) = features(&Value::Null);
        assert_eq!(with_screens_if(reads, vec![], &mirrors, &end), [json!({"type": "local.device.heartbeat", "sessions": []})]);
        assert_eq!(with_screens_if(reads, vec![json!({"type": "x"})], &mirrors, &end).len(), 1, "a heartbeat goes anyway: its ack will say");
        assert!(with_screens_if(reads, vec![], &BTreeMap::new(), &end).is_empty(), "no kept screen, nothing to ask for");
        // An older relay's ack lists nothing: nothing is learned and no `screens` is sent.
        let old = json!({"type": "local.device.heartbeat.ack", "rejected": [], "reconcile": [], "serverTime": 1});
        assert_eq!(ack_features(&old), None);
        // This relay's does: the next heartbeat names the screens again.
        let (reads, reason) = ack_features(&json!({"type": "local.device.heartbeat.ack", "features": ["endedScreens"]})).unwrap();
        assert!(reads && !reason);
        assert_eq!(with_screens_if(reads, vec![], &mirrors, &end)[0]["screens"], json!([SID]));
        // An ack that lists features without it turns it off.
        assert_eq!(ack_features(&json!({"features": []})), Some((false, false)));
    }

    #[test]
    fn the_screens_frame_is_the_golden_one() {
        assert_eq!(screens_frame(&[SID.into()]), serde_json::from_str::<Value>(GOLDEN).unwrap());
    }

    #[test]
    fn an_ended_holder_is_closed_unless_a_running_session_or_a_reported_screen_names_it() {
        let h = |id: &str, pid| HolderInfo { id: id.into(), socket: format!("/x/{id}.sock").into(), pid, provider_pid: 2,
            argv: vec![], cwd: "/".into(), watch: true, type_input: true };
        let holders = [h("h1", 10), h("fresh", 11), h("h3", 12), h("livehold", 13)];
        let mut live = test_mirror("thread-live", "gen-1");
        live.native.holder = Some("livehold".into());
        let mirrors = BTreeMap::from([
            ("a".to_string(), ended("h1", 100, SID)),
            ("c".to_string(), ended("h3", 50, "left-earlier")),
            ("j".to_string(), live),
        ]);
        let named = named(&mirrors, &kept(&mirrors, &|_: &str| Some(101_000)));
        // `fresh` runs (no mirror names it yet); `h3` shows a screen no session may be shown.
        assert_eq!(orphans(&holders, &named, |x: &HolderInfo| x.id != "fresh"), vec![12]);
    }

    #[test]
    fn a_screen_is_news_once_when_it_is_first_kept() {
        let mut shown = BTreeSet::new();
        assert_eq!(fresh(&mut shown, BTreeSet::from(["a".to_string()])), ["a"]);
        assert!(fresh(&mut shown, BTreeSet::from(["a".to_string()])).is_empty());
        assert_eq!(fresh(&mut shown, BTreeSet::from(["a".to_string(), "b".to_string()])), ["b"]);
        assert!(fresh(&mut shown, BTreeSet::new()).is_empty());
        assert_eq!(fresh(&mut shown, BTreeSet::from(["a".to_string()])), ["a"], "kept again after it was gone");
    }

    #[test]
    fn a_running_session_without_a_terminal_says_why() {
        let mut m = test_mirror("thread-1", "gen-1"); // Codex, no holder, no job
        m.native.process.executable = "/opt/homebrew/bin/codex".into();
        assert_eq!(m.native.terminal_reason(), Some("no-daemon"));
        m.native.process.executable = "/Applications/ChatGPT.app/Contents/Resources/codex".into();
        assert_eq!(m.native.terminal_reason(), Some("unknown"), "the Codex app is S7's `app`");
        m.native.provider = "claude".into();
        assert_eq!(m.native.terminal_reason(), Some("plain"));
        m.native.job = Some("8ed71738".into());
        assert_eq!(m.native.terminal_reason(), None, "attach");
        m.native.job = None;
        m.native.holder = Some("ab12cd34".into());
        assert_eq!(m.native.terminal_reason(), None, "held");
    }

    #[test]
    fn the_new_fields_go_only_to_a_relay_that_lists_them() {
        // The pure halves: the statics are one connection's and other tests' heartbeats read them.
        assert_eq!(features(&json!(["sessionTerminal"])), (false, false));
        assert_eq!(features(&json!(["terminalReason", "endedScreens"])), (true, true));
        assert_eq!(features(&Value::Null), (false, false));
        let mut m = test_mirror("thread-1", "gen-1");
        m.native.provider = "claude".into();
        let mirrors = BTreeMap::from([("a".to_string(), ended("ab12cd34", 5, SID))]);
        let end = |_: &str| Some(5_000);
        let mut entry = json!({});
        add_reason_if(false, &mut entry, &m.native);
        assert!(entry.get("terminalReason").is_none());
        assert_eq!(with_screens_if(false, vec![json!({"type": "x"})], &mirrors, &end).len(), 1);
        let mut frame = json!({"type": "local.session.ended"});
        add_screen_if(false, &mut frame, &mirrors, "a", &end);
        assert!(frame.get("screen").is_none());
        add_reason_if(true, &mut entry, &m.native);
        assert_eq!(entry["terminalReason"], "plain");
        assert_eq!(with_screens_if(true, vec![json!({"type": "x"})], &mirrors, &end)[1]["screens"], json!([SID]));
        assert_eq!(with_screens_if(true, vec![], &BTreeMap::new(), &end).len(), 0, "no kept screen, no frame");
        add_screen_if(true, &mut frame, &mirrors, "a", &|_: &str| None);
        assert!(frame.get("screen").is_none(), "its holder still runs, or is gone");
        add_screen_if(true, &mut frame, &mirrors, "a", &end);
        assert_eq!(frame["screen"], true);
    }
}
