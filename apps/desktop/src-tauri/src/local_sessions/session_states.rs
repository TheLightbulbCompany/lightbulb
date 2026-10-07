//! Session states beyond a running terminal (spec 2026-10-07 §5.1, §5.4): an ended held session's
//! final screen, which its holder keeps while this app runs, and why a running session has no
//! terminal. `transport.rs` calls in here at six one-line seams; `screen.rs` is untouched: a kept
//! ended screen is indexed as live, so a viewer's watch reaches its holder like a running one's.

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

/// (`endedScreens`, `terminalReason`) as a registration ack's `features` lists them.
fn features(features: &Value) -> (bool, bool) {
    let has = |name: &str| features.as_array().is_some_and(|f| f.iter().any(|x| x == name));
    (has("endedScreens"), has("terminalReason"))
}

/// A registration ack's `features`. Per connection, like the other `relay_reads_*` flags: every
/// heartbeat follows a registration ack on the same connection.
pub(super) fn read_features(listed: &Value) {
    let (screens, reason) = features(listed);
    READS_ENDED_SCREENS.store(screens, SeqCst);
    READS_TERMINAL_REASON.store(reason, SeqCst);
}

/// The holder still listens: its final screen is there. A connect only, never a wait on a reply.
fn holder_listens(id: &str) -> bool {
    UnixStream::connect(manager::socket_dir().join(format!("{id}.sock"))).is_ok()
}

/// An ended, registered held session whose holder still shows its final screen.
fn keeps_screen(m: &Mirror, listens: &impl Fn(&str) -> bool) -> bool {
    m.ended_at.is_some() && m.session_id.is_some() && m.native.holder.as_deref().is_some_and(listens)
}

/// The ended screens this Mac still shows, newest end first, one frame's worth.
fn screens(mirrors: &BTreeMap<String, Mirror>, listens: &impl Fn(&str) -> bool) -> Vec<String> {
    let mut kept: Vec<&Mirror> = mirrors.values().filter(|m| keeps_screen(m, listens)).collect();
    kept.sort_by_key(|m| std::cmp::Reverse(m.ended_at));
    kept.into_iter().take(SCREENS_MAX).filter_map(|m| m.session_id.clone()).collect()
}

fn screens_frame(sids: &[String]) -> Value {
    json!({"type": "local.device.heartbeat", "sessions": [], "screens": sids})
}

/// The heartbeat's frames, plus one naming the kept ended screens for a relay that reads it.
pub(super) fn with_screens(frames: Vec<Value>, mirrors: &BTreeMap<String, Mirror>) -> Vec<Value> {
    with_screens_if(READS_ENDED_SCREENS.load(SeqCst), frames, mirrors, &holder_listens)
}

fn with_screens_if(reads: bool, mut frames: Vec<Value>, mirrors: &BTreeMap<String, Mirror>, listens: &impl Fn(&str) -> bool) -> Vec<Value> {
    if reads {
        let sids = screens(mirrors, listens);
        if !sids.is_empty() {
            frames.push(screens_frame(&sids));
        }
    }
    frames
}

/// `local.session.ended` says the final screen stays, so viewers keep it without waiting for a heartbeat.
pub(super) fn add_screen(frame: &mut Value, mirror: &Mirror) {
    add_screen_if(READS_ENDED_SCREENS.load(SeqCst), frame, mirror, &holder_listens);
}

fn add_screen_if(reads: bool, frame: &mut Value, mirror: &Mirror, listens: &impl Fn(&str) -> bool) {
    if reads && keeps_screen(mirror, listens) {
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

/// `screen::index` with every kept ended screen indexed as live; then holders no mirror names are closed.
pub(super) fn index(mirrors: &BTreeMap<String, Mirror>) {
    let shown = with_kept(mirrors, &holder_listens);
    super::screen::index(shown.as_ref().unwrap_or(mirrors));
    sweep(mirrors.values().filter_map(|m| m.native.holder.clone()).collect());
}

/// The mirrors the screen index should see; None when no ended screen is kept (nothing to copy).
// ponytail: clones the live mirrors once a second while a screen is kept; an index that took ids would not (screen.rs is S2's).
fn with_kept(mirrors: &BTreeMap<String, Mirror>, listens: &impl Fn(&str) -> bool) -> Option<BTreeMap<String, Mirror>> {
    if !mirrors.values().any(|m| keeps_screen(m, listens)) {
        return None;
    }
    Some(
        mirrors
            .iter()
            .filter(|(_, m)| m.ended_at.is_none() || keeps_screen(m, listens))
            .map(|(k, m)| (k.clone(), Mirror { ended_at: None, ..m.clone() }))
            .collect(),
    )
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

/// Ended holders no mirror names: a resumed session's old holder once discovery names the new
/// one, or a session reaped after a day. A running holder nobody names yet (just started) never is.
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

    fn ended(holder: &str, at: u64, sid: &str) -> Mirror {
        let mut m = test_mirror(&format!("thread-{holder}"), "gen-1");
        m.session_id = Some(sid.into());
        m.native.holder = Some(holder.into());
        m.ended_at = Some(at);
        m
    }

    #[test]
    fn an_ended_screen_is_kept_only_while_its_holder_listens_and_is_indexed_as_live() {
        let mut live = test_mirror("thread-live", "gen-1");
        live.session_id = Some("live".into());
        let mirrors = BTreeMap::from([
            ("a".to_string(), ended("ab12cd34", 5, SID)),
            ("b".to_string(), ended("dead0000", 9, "gone")),
            ("c".to_string(), live),
        ]);
        let listens = |id: &str| id == "ab12cd34";
        assert_eq!(screens(&mirrors, &listens), vec![SID.to_string()]);
        let shown = with_kept(&mirrors, &listens).unwrap();
        assert_eq!(shown.keys().cloned().collect::<Vec<_>>(), ["a", "c"]);
        assert!(shown.values().all(|m| m.ended_at.is_none()));
        assert!(with_kept(&mirrors, &|_: &str| false).is_none(), "nothing kept: the mirrors go in as they are");
    }

    #[test]
    fn screens_are_newest_first_and_one_frame_at_most() {
        let mirrors: BTreeMap<String, Mirror> =
            (0..30u64).map(|i| (format!("k{i}"), ended(&format!("{i:08}"), i, &format!("s{i}")))).collect();
        let sids = screens(&mirrors, &|_: &str| true);
        assert_eq!(sids.len(), 24);
        assert_eq!(sids[0], "s29");
    }

    #[test]
    fn the_screens_frame_is_the_golden_one() {
        assert_eq!(screens_frame(&[SID.into()]), serde_json::from_str::<Value>(GOLDEN).unwrap());
    }

    #[test]
    fn only_an_ended_holder_no_mirror_names_is_closed() {
        let h = |id: &str, pid| HolderInfo { id: id.into(), socket: format!("/x/{id}.sock").into(), pid, provider_pid: 2,
            argv: vec![], cwd: "/".into(), watch: true, type_input: true };
        let holders = [h("named000", 10), h("fresh000", 11), h("old00000", 12)];
        let named = BTreeSet::from(["named000".to_string()]);
        assert_eq!(orphans(&holders, &named, |x: &HolderInfo| x.id != "fresh000"), vec![12]);
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
        let kept = BTreeMap::from([("a".to_string(), ended("ab12cd34", 5, SID))]);
        let listens = |_: &str| true;
        let mut entry = json!({});
        add_reason_if(false, &mut entry, &m.native);
        assert!(entry.get("terminalReason").is_none());
        assert_eq!(with_screens_if(false, vec![json!({"type": "x"})], &kept, &listens).len(), 1);
        let mut end = json!({"type": "local.session.ended"});
        add_screen_if(false, &mut end, &kept["a"], &listens);
        assert!(end.get("screen").is_none());
        add_reason_if(true, &mut entry, &m.native);
        assert_eq!(entry["terminalReason"], "plain");
        assert_eq!(with_screens_if(true, vec![json!({"type": "x"})], &kept, &listens)[1]["screens"], json!([SID]));
        assert_eq!(with_screens_if(true, vec![], &BTreeMap::new(), &listens).len(), 0, "no kept screen, no frame");
        add_screen_if(true, &mut end, &kept["a"], &listens);
        assert_eq!(end["screen"], true);
        add_screen_if(true, &mut end, &kept["a"], &|_: &str| false);
    }
}
