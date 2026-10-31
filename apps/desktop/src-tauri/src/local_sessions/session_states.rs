//! Session states beyond a running terminal (spec 2026-10-07 §5.1, §5.4): an ended held session's
//! final screen, which its holder keeps while this app runs, and why a running session has no
//! terminal. `transport.rs` calls in here at eight one-line seams. A kept ended screen is indexed as live,
//! so a viewer's watch reaches its holder like a running one's; a relay already on the holder
//! stays through the end (`screen::pump`), and one started just after waits on `is_settling`.
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
/// The relay lists `endedOnPurpose`: an end may say `reason: "on-purpose"`.
static READS_ON_PURPOSE: AtomicBool = AtomicBool::new(false);
/// The relay lists `endedIdleStopped`: an end may say `reason: "idle-stopped"`. Until it does, no
/// idle session is stopped (`Host::stop_idle`): its row would read as an ordinary exit.
static READS_IDLE_STOPPED: AtomicBool = AtomicBool::new(false);
/// The relay lists `remoteResize`: it reads `remoteResize` on a heartbeat and may send a
/// `local.session.input` with the app panel's `size` (owner ruling 2026-10-14).
static READS_REMOTE_RESIZE: AtomicBool = AtomicBool::new(false);
/// The relay lists `sessionAttention`: it reads `done`, `waitingFor` and `note`.
static READS_ATTENTION: AtomicBool = AtomicBool::new(false);
/// The relay lists `networkSessionId`: an older one's `extra="forbid"` rejects a frame carrying it.
static READS_NETWORK_ID: AtomicBool = AtomicBool::new(false);
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
/// How often the holders of ended sessions are asked whether their provider exited.
const PROBE_EVERY: Duration = Duration::from_secs(2);
/// The same, while a session that just ended waits for its holder's answer (`settling`).
const PROBE_SOON: Duration = Duration::from_millis(300);
/// A session that ended this recently may yet keep its screen: its holder's provider is still
/// shutting down, or the pass that asks it has not come round.
const SETTLE_MS: u64 = ENDED_WITH_MS + 5_000;
/// An ended holder is closed no sooner than this after its provider exited, whatever is known.
const SWEEP_AFTER_MS: u64 = 30_000;
/// How long a held Codex session's end waits for its holder to say how its provider left.
const EXIT_WAIT_MS: u64 = 4_000;
/// The holders whose provider left by itself with status 0 (`manager::ended`).
// ponytail: one id per such end until the app quits; prune with ENDS if that ever matters.
static LEFT_CLEAN: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());
/// The registered sessions the last `index` found `settling`.
static SETTLING: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());

/// Holder id -> when its provider exited, as last asked. The relay loop only reads this: asking
/// is a socket round trip per holder with a 2 s read timeout, and the loop that waited for one
/// sent no heartbeat meanwhile. `refresh` asks on its own thread.
struct Ends {
    at: Mutex<BTreeMap<String, u64>>,
    asked: Mutex<Option<Instant>>,
    asking: AtomicBool,
}
static ENDS: Ends = Ends::new();

impl Ends {
    const fn new() -> Self {
        Self { at: Mutex::new(BTreeMap::new()), asked: Mutex::new(None), asking: AtomicBool::new(false) }
    }

    fn get(&self, id: &str) -> Option<u64> {
        self.at.lock().ok()?.get(id).copied()
    }

    /// Asks every one of `holders` once, off the caller's thread: at most once in `every`, and
    /// never beside a pass that has not finished (a holder that does not answer holds one thread).
    fn refresh(&'static self, holders: BTreeSet<String>, every: Duration, ask: impl Fn(&str) -> Option<u64> + Send + 'static) {
        let Ok(mut asked) = self.asked.lock() else { return };
        if asked.is_some_and(|at| at.elapsed() < every) {
            return;
        }
        if holders.is_empty() {
            // No ended session names a holder: nothing to ask, and no thread every two seconds.
            if let Ok(mut at) = self.at.lock() {
                at.clear();
            }
            return;
        }
        if self.asking.swap(true, SeqCst) {
            return;
        }
        *asked = Some(Instant::now());
        drop(asked);
        std::thread::spawn(move || {
            let found: BTreeMap<String, u64> = holders.into_iter().filter_map(|id| ask(&id).map(|at| (id, at))).collect();
            if let Ok(mut at) = self.at.lock() {
                *at = found;
            }
            self.asking.store(false, SeqCst);
        });
    }
}
/// The mirrors whose screen the last `index` kept.
static SHOWN: Mutex<BTreeSet<String>> = Mutex::new(BTreeSet::new());
static ENDED_NOW: AtomicBool = AtomicBool::new(false);

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
    READS_ON_PURPOSE.store(listed.as_array().is_some_and(|f| f.iter().any(|x| x == "endedOnPurpose")), SeqCst);
    READS_IDLE_STOPPED.store(listed.as_array().is_some_and(|f| f.iter().any(|x| x == "endedIdleStopped")), SeqCst);
    READS_REMOTE_RESIZE.store(listed.as_array().is_some_and(|f| f.iter().any(|x| x == "remoteResize")), SeqCst);
    READS_ATTENTION.store(listed.as_array().is_some_and(|f| f.iter().any(|x| x == "sessionAttention")), SeqCst);
    READS_NETWORK_ID.store(lists_network_id(listed), SeqCst);
    super::signals::read_features(listed);
}

/// What Claude says beyond `status` (`claude::Attention`), on a register or heartbeat entry, for a
/// relay that reads it. Absent is "none": each frame says all of it.
pub(super) fn add_attention(entry: &mut Value, native: &NativeSession) {
    add_attention_if(READS_ATTENTION.load(SeqCst), entry, native);
}

fn add_attention_if(reads: bool, entry: &mut Value, native: &NativeSession) {
    let a = &native.attention;
    if !reads {
        return;
    }
    if a.done {
        entry["done"] = json!(true);
    }
    if let Some(reason) = &a.waiting_for {
        entry["waitingFor"] = json!(reason);
    }
    if let Some(note) = &a.note {
        entry["note"] = json!(note);
    }
}

fn lists_network_id(listed: &Value) -> bool {
    listed.as_array().is_some_and(|f| f.iter().any(|x| x == "networkSessionId"))
}

/// The engine's key for this session (`Mirror::network_id`), on a register or heartbeat entry.
pub(super) fn add_network_id(entry: &mut Value, mirror: &Mirror) {
    add_network_id_if(READS_NETWORK_ID.load(SeqCst), entry, mirror);
}

fn add_network_id_if(reads: bool, entry: &mut Value, mirror: &Mirror) {
    if let Some(id) = mirror.network_id.as_ref().filter(|_| reads) {
        entry["networkSessionId"] = json!(id);
    }
}

/// The relay this Mac talks to now reads `remoteResize`.
pub(super) fn reads_remote_resize() -> bool {
    READS_REMOTE_RESIZE.load(SeqCst)
}

/// The relay this Mac talks to now reads an `idle-stopped` end.
pub(super) fn reads_idle_stopped() -> bool {
    READS_IDLE_STOPPED.load(SeqCst)
}

#[cfg(test)]
pub(super) fn set_reads_network_id(on: bool) {
    READS_NETWORK_ID.store(on, SeqCst);
}

/// Only this flag: `read_features` would reset the others under parallel tests.
#[cfg(test)]
pub(super) fn set_reads_idle_stopped(on: bool) {
    READS_IDLE_STOPPED.store(on, SeqCst);
}

/// The reason `local.session.ended` gives. "on-purpose" when its person ended it: the provider's
/// own exit (`lifecycle::ON_PURPOSE`), End asked of its holder (or of the session, when no holder
/// runs it) at or before the end, or a held Codex that quit by itself. Only for a relay that reads
/// it; an older one hears "exited" (or "process-exited") as before.
pub(super) fn end_reason(mirror: &Mirror) -> &str {
    idle_reason_if(READS_IDLE_STOPPED.load(SeqCst), end_reason_if(READS_ON_PURPOSE.load(SeqCst), mirror, &manager::end_asked, &left_clean))
}

/// Only a relay that said it reads `idle-stopped` is sent it: a reconnect clears the negotiated
/// features, and an older relay rejects a reason it does not know (as `on-purpose` falls back).
fn idle_reason_if(reads: bool, reason: &str) -> &str {
    if reason == super::lifecycle::IDLE_STOPPED && !reads { "exited" } else { reason }
}

/// When this holder's provider left by itself with status 0 (unix ms), as last asked.
fn left_clean(id: &str) -> Option<u64> {
    holder_end(id).filter(|_| LEFT_CLEAN.lock().is_ok_and(|left| left.contains(id)))
}

/// A held Codex session just ended and its holder has not said how its provider left: its end
/// is reported once it has (`end_reason` reads that), or EXIT_WAIT_MS after it.
// ponytail: `/new` in a held Codex tells the thread it left EXIT_WAIT_MS late (its holder runs on).
pub(super) fn awaits_exit(mirror: &Mirror) -> bool {
    READS_ON_PURPOSE.load(SeqCst) && awaits_exit_if(mirror, &holder_end, now_ms())
}

fn awaits_exit_if(mirror: &Mirror, end: &impl Fn(&str) -> Option<u64>, now_ms: u64) -> bool {
    mirror.native.provider == "codex"
        && mirror.native.holder.as_deref().is_some_and(|id| end(id).is_none())
        && mirror.ended_at_ms.is_some_and(|at| at + EXIT_WAIT_MS > now_ms)
}

/// End was asked of its holder or its session at or before this end.
fn asked_before_end(mirror: &Mirror, asked: &impl Fn(&str) -> Option<u64>) -> bool {
    [mirror.native.holder.as_deref(), mirror.session_id.as_deref()]
        .into_iter()
        .flatten()
        .filter_map(asked)
        .any(|at| mirror.ended_at.is_some_and(|ended| ended >= at))
}

/// A reason that says someone meant this end: its person, or the app ending a stranded or idle holder.
pub(super) fn meant_reason(reason: &str) -> bool {
    [super::lifecycle::ON_PURPOSE, super::lifecycle::IDLE_STOPPED, super::lifecycle::LOGIN_SESSION_ENDED].contains(&reason)
}

/// This ended mirror's end was meant, so a row still listed for a moment does not bring it back.
pub(super) fn end_was_meant(mirror: &Mirror) -> bool {
    mirror.ended_reason.as_deref().is_some_and(meant_reason) || asked_before_end(mirror, &manager::end_asked)
}

fn end_reason_if<'a>(reads: bool, mirror: &'a Mirror, asked: &impl Fn(&str) -> Option<u64>, left: &impl Fn(&str) -> Option<u64>) -> &'a str {
    let on_purpose = super::lifecycle::ON_PURPOSE;
    let stored = mirror.ended_reason.as_deref().unwrap_or("exited");
    // The app's own END of a stranded or idle holder marks it asked; that is not a person's End.
    if stored == super::lifecycle::LOGIN_SESSION_ENDED || stored == super::lifecycle::IDLE_STOPPED {
        return stored;
    }
    let holder = mirror.native.holder.as_deref();
    let ended_as_asked = asked_before_end(mirror, asked);
    // Codex names no reason for its end (`lifecycle::transition`). Its own quit (`/exit`, ctrl+c)
    // is its holder's provider leaving with status 0, and the session with it; a crash or a kill
    // is never 0, and `/new` or `/resume` leave the holder running.
    let ended_ms = mirror.ended_at_ms.or(mirror.ended_at.map(|at| at * 1000));
    let quit = mirror.native.provider == "codex"
        && holder.and_then(left).is_some_and(|exited| ended_ms.is_some_and(|ended| ended + ENDED_WITH_MS >= exited));
    let purposeful = stored == on_purpose || ended_as_asked || quit;
    match (purposeful && reads, stored == on_purpose) {
        (true, _) => on_purpose,
        (false, true) => "exited",
        (false, false) => stored,
    }
}

#[cfg(test)]
pub(super) fn reads_ended_screens() -> bool {
    READS_ENDED_SCREENS.load(SeqCst)
}

/// When this holder's provider exited (unix ms), if the holder showed only its final screen when
/// last asked (up to PROBE_EVERY and one pass ago). Never a socket: this runs on the relay loop.
fn holder_end(id: &str) -> Option<u64> {
    ENDS.get(id)
}

/// A relay read this holder's EXIT: its provider exited now. Known here at once, not when the
/// next pass has asked the holder (up to PROBE_SOON and an `index` later): a viewer waits that
/// long for the kept picture. A pass already under way may still drop it; the one after finds it.
pub(super) fn holder_ended(id: &str, clean: bool) {
    // How it left first: the relay loop must never read the end as known and not yet clean.
    note_clean(id, clean);
    if let Ok(mut at) = ENDS.at.lock() {
        at.entry(id.to_owned()).or_insert_with(now_ms);
    }
    ENDED_NOW.store(true, SeqCst);
}

/// `clean`: this holder's provider left by itself with status 0.
fn note_clean(id: &str, clean: bool) {
    if clean {
        if let Ok(mut left) = LEFT_CLEAN.lock() {
            left.insert(id.to_owned());
        }
    }
}

/// A relay read a holder's EXIT since the last call: the relay loop indexes now, not at its next
/// one-second pass.
pub(super) fn ended_now() -> bool {
    ENDED_NOW.swap(false, SeqCst)
}

/// One holder, asked on `Ends::refresh`'s thread. An end never changes, so a known one is only
/// checked for still being there (a closed holder's screen is gone).
fn ask_holder(id: &str) -> Option<u64> {
    let socket = manager::socket_dir().join(format!("{id}.sock"));
    match ENDS.get(id) {
        Some(at) => UnixStream::connect(&socket).ok().map(|_| at),
        // (`note_clean` before `refresh` stores the end, for the same reason as in `holder_ended`.)
        None => manager::ended(&socket).map(|(at, clean)| {
            note_clean(id, clean);
            at
        }),
    }
}

/// The holders an ended session was last seen in: the only ones whose end matters here.
fn ended_holders(mirrors: &BTreeMap<String, Mirror>) -> BTreeSet<String> {
    mirrors.values().filter(|m| m.ended_at.is_some()).filter_map(|m| m.native.holder.clone()).collect()
}

/// The ended sessions whose final screen this Mac still shows, by mirror key, newest end first, one
/// frame's worth: registered, and the only session that ended with its holder's provider. `end`:
/// when a holder's provider exited (None while it runs, or once the holder is closed).
fn kept<'a>(mirrors: &'a BTreeMap<String, Mirror>, end: &impl Fn(&str) -> Option<u64>) -> Vec<(&'a str, &'a Mirror)> {
    let mut by_holder: BTreeMap<&str, Vec<(&str, &Mirror)>> = BTreeMap::new();
    for (key, m) in mirrors {
        let (Some(at), Some(holder)) = (m.ended_at, m.native.holder.as_deref()) else { continue };
        if super::transport::stopped(key, m) {
            continue; // not shared: its final screen is not shown either
        }
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

/// The frame that names the kept ended screens, by itself: a screen is newly kept, and its viewers
/// wait for this report (the backend forwards none of an ended session's frames before it, and
/// then asks for the picture). None for a relay that does not read it. Prod 2026-10-08: the report
/// went behind the device heartbeat's other frames, one ack each, 1.2 s that time.
pub(super) fn screens_now(mirrors: &BTreeMap<String, Mirror>) -> Option<Value> {
    with_screens_if(READS_ENDED_SCREENS.load(SeqCst), Vec::new(), mirrors, &holder_end).pop().filter(|frame| frame.get("screens").is_some())
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
/// session's screen are closed. Returns the mirrors whose screen is newly kept: the caller reports
/// them now (`screens_now`), or brings the heartbeat forward for a relay that reads no such frame.
pub(super) fn index(mirrors: &BTreeMap<String, Mirror>) -> Vec<String> {
    let soon = SETTLING.lock().is_ok_and(|settling| !settling.is_empty());
    ENDS.refresh(ended_holders(mirrors), if soon { PROBE_SOON } else { PROBE_EVERY }, ask_holder);
    let kept = kept(mirrors, &holder_end);
    let shown = with_kept(mirrors, &holder_end);
    super::screen::index(shown.as_ref().unwrap_or(mirrors));
    if let Ok(mut waiting) = SETTLING.lock() {
        *waiting = settling(mirrors, &kept, now_ms());
    }
    sweep(named(mirrors, &kept), unknown(mirrors, &holder_end));
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

/// The holders an ended session names whose end is not known: their provider still runs, they are
/// closed, or nothing has asked them since it exited. Only the last can be an ended holder, and
/// that one may be about to show its session's screen.
fn unknown(mirrors: &BTreeMap<String, Mirror>, end: &impl Fn(&str) -> Option<u64>) -> BTreeSet<String> {
    ended_holders(mirrors).into_iter().filter(|id| end(id).is_none()).collect()
}

fn now_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64)
}

/// The registered sessions that ended in the last SETTLE_MS in a holder and keep no screen yet.
fn settling(mirrors: &BTreeMap<String, Mirror>, kept: &[(&str, &Mirror)], now_ms: u64) -> BTreeSet<String> {
    mirrors
        .iter()
        .filter(|(key, m)| m.native.holder.is_some() && !kept.iter().any(|(k, _)| k == key))
        .filter(|(_, m)| m.ended_at.is_some_and(|at| m.ended_at_ms.unwrap_or(at * 1000) + SETTLE_MS > now_ms))
        .filter_map(|(_, m)| m.session_id.clone())
        .collect()
}

/// Whether this session just ended in a holder that may yet keep its screen (as of the last
/// `index`): a relay starting now waits for the answer instead of finding no terminal.
pub(super) fn is_settling(session_id: &str) -> bool {
    SETTLING.lock().is_ok_and(|settling| settling.contains(session_id))
}

/// Off the relay loop, at most every SWEEP_EVERY: `list` and `ended_at` talk to every holder.
fn sweep(named: BTreeSet<String>, unknown: BTreeSet<String>) {
    if cfg!(test) {
        return; // never signal the developer's own holders
    }
    let Ok(mut last) = SWEPT.lock() else { return };
    if last.is_some_and(|at| at.elapsed() < SWEEP_EVERY) {
        return;
    }
    *last = Some(Instant::now());
    std::thread::spawn(move || {
        let holders = manager::list();
        for pid in orphans(&holders, &named, &unknown, |h| manager::ended_at(&h.socket), now_ms()) {
            let id = holders.iter().find(|h| h.pid == pid).map_or("", |h| h.id.as_str());
            super::transport::relay_log(&format!("closed ended holder {id}: no session shows its screen"));
            manager::stop(pid);
        }
    });
}

/// Ended holders no session needs: a resumed session's old holder once discovery names the new
/// one, a session reaped after a day, a screen past the newest SCREENS_MAX, or a final screen
/// that is no session's to show. A running holder nobody names yet (just started) never is, nor
/// one that ended (`ended`: when, unix ms) under SWEEP_AFTER_MS ago, nor one whose screen is
/// `unknown`: not known yet is not "no session's screen".
fn orphans(holders: &[HolderInfo], named: &BTreeSet<String>, unknown: &BTreeSet<String>, ended: impl Fn(&HolderInfo) -> Option<u64>, now_ms: u64) -> Vec<u32> {
    holders
        .iter()
        .filter(|h| !named.contains(&h.id) && !unknown.contains(&h.id))
        .filter(|h| ended(h).is_some_and(|at| at + SWEEP_AFTER_MS <= now_ms))
        .map(|h| h.pid)
        .collect()
}

#[cfg(test)]
mod tests {

    #[test]
    fn session_states_attention_travels_only_to_a_relay_that_reads_it() {
        let mut native: NativeSession = serde_json::from_value(json!({"provider":"claude","conversation":"c","profile":"p",
            "process":{"pid":1,"birth_micros":1,"executable":"/x"},"transcript":null,"socket":null,"socket_inode":null,"version":"2.1.310",
            "restriction":null,"status":"idle"})).unwrap();
        native.attention = super::super::claude::Attention { done: true, waiting_for: None, note: Some("press Resume".into()), agents: 0 };
        let mut entry = json!({});
        add_attention_if(false, &mut entry, &native);
        assert_eq!(entry, json!({}));
        add_attention_if(true, &mut entry, &native);
        assert_eq!(entry, json!({"done": true, "note": "press Resume"}));
    }
    use super::super::transport::tests::test_mirror;
    use super::*;

    #[test]
    fn session_states_the_network_id_travels_only_to_a_relay_that_lists_it() {
        assert!(lists_network_id(&json!(["x", "networkSessionId"])));
        assert!(!lists_network_id(&json!(["x"])) && !lists_network_id(&Value::Null));
        let mut mirror = test_mirror(SID, "g");
        let mut entry = json!({});
        add_network_id_if(true, &mut entry, &mirror);
        assert_eq!(entry, json!({}), "none yet");
        mirror.network_id = Some("abc/claude:u".into());
        add_network_id_if(false, &mut entry, &mirror);
        assert_eq!(entry, json!({}), "a relay that does not list it");
        add_network_id_if(true, &mut entry, &mirror);
        assert_eq!(entry, json!({"networkSessionId": "abc/claude:u"}));
    }

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

    #[test]
    fn an_end_is_on_purpose_only_for_the_tools_own_exit_or_an_end_asked_of_its_holder() {
        let none = |_: &str| None;
        let asked = |id: &str| (id == "h1").then_some(100);
        // (No holder here says how its provider left.)
        fn end_reason_if<'a>(reads: bool, mirror: &'a Mirror, asked: &impl Fn(&str) -> Option<u64>) -> &'a str {
            super::end_reason_if(reads, mirror, asked, &|_: &str| None)
        }
        let mut left = ended("h1", 100, SID);
        left.ended_reason = Some("on-purpose".into());
        assert_eq!(end_reason_if(true, &left, &none), "on-purpose", "`/exit`");
        assert_eq!(end_reason_if(false, &left, &none), "exited", "an older relay refuses a reason it does not know");
        let mut hung_up = ended("h1", 100, SID);
        hung_up.ended_reason = Some("exited".into());
        assert_eq!(end_reason_if(true, &hung_up, &none), "exited", "a closed terminal, a kill or a shutdown");
        assert_eq!(end_reason_if(true, &hung_up, &asked), "on-purpose", "End");
        let mut killed = ended("h1", 105, SID);
        killed.ended_reason = Some("process-exited".into());
        assert_eq!(end_reason_if(true, &killed, &asked), "on-purpose", "End, and the provider had to be killed");
        assert_eq!(end_reason_if(false, &killed, &asked), "process-exited");
        assert_eq!(end_reason_if(true, &killed, &none), "process-exited", "a crash");
        // A conversation that left this holder before End was asked did not end by it.
        let mut earlier = ended("h1", 99, SID);
        earlier.ended_reason = Some("exited".into());
        assert_eq!(end_reason_if(true, &earlier, &asked), "exited");
        // No holder runs it (a plain Claude, a Claude background session): End is asked of the session.
        let mut plain = ended("h1", 100, SID);
        plain.native.holder = None;
        plain.ended_reason = Some("process-exited".into());
        assert_eq!(end_reason_if(true, &plain, &|id: &str| (id == SID).then_some(100)), "on-purpose", "End");
        assert_eq!(end_reason_if(true, &plain, &asked), "process-exited", "another session's End");
    }

    #[test]
    fn a_session_ended_for_its_login_session_says_so_though_the_app_asked_its_holder_to_end() {
        // The stranded END asks the holder in the same second the end is recorded; that ask is the
        // app's, not a person's, so the end is not on purpose.
        let asked = |id: &str| (id == "h1").then_some(100);
        let left = |id: &str| (id == "h1").then_some(100_500);
        let mut stranded = ended("h1", 100, SID);
        stranded.native.provider = "codex".into();
        stranded.ended_reason = Some("login-session-ended".into());
        assert_eq!(end_reason_if(true, &stranded, &asked, &left), "login-session-ended");
        // So is the app's END of a session left idle with nobody on it.
        stranded.ended_reason = Some("idle-stopped".into());
        assert_eq!(end_reason_if(true, &stranded, &asked, &left), "idle-stopped");
        // Sent only to a relay that said it reads it; otherwise a reason every relay knows.
        assert_eq!(idle_reason_if(true, "idle-stopped"), "idle-stopped");
        assert_eq!(idle_reason_if(false, "idle-stopped"), "exited");
        assert_eq!(idle_reason_if(false, "on-purpose"), "on-purpose");
    }

    #[test]
    fn a_held_codex_that_quit_by_itself_ended_on_purpose_and_its_end_waits_to_learn_that() {
        let none = |_: &str| None;
        // Its holder's provider left with status 0 at 100.5 s.
        let left = |id: &str| (id == "h1").then_some(100_500);
        let mut quit = ended("h1", 100, SID);
        quit.native.provider = "codex".into();
        quit.ended_reason = Some("exited".into());
        assert_eq!(end_reason_if(true, &quit, &none, &left), "on-purpose", "`/exit`");
        assert_eq!(end_reason_if(false, &quit, &none, &left), "exited", "an older relay");
        assert_eq!(end_reason_if(true, &quit, &none, &none), "exited", "a crash, a kill, or a holder that runs on");
        // `/new` long before the quit: that thread did not end with the provider.
        let mut before = ended("h1", 80, SID);
        before.native.provider = "codex".into();
        assert_eq!(end_reason_if(true, &before, &none, &left), "exited");
        let mut claude = ended("h1", 100, SID);
        claude.native.provider = "claude".into();
        claude.ended_reason = Some("exited".into());
        assert_eq!(end_reason_if(true, &claude, &none, &left), "exited", "Claude names its own reason");

        // The end is held until the holder has answered, and no longer than EXIT_WAIT_MS.
        assert!(awaits_exit_if(&quit, &none, 100_000 + EXIT_WAIT_MS - 1));
        assert!(!awaits_exit_if(&quit, &none, 100_000 + EXIT_WAIT_MS));
        assert!(!awaits_exit_if(&quit, &left, 100_100), "known");
        assert!(!awaits_exit_if(&claude, &none, 100_100));
        quit.native.holder = None;
        assert!(!awaits_exit_if(&quit, &none, 100_100), "no holder to ask");
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

    fn asked_long_ago(ends: &Ends) {
        *ends.asked.lock().unwrap() = None;
    }

    #[test]
    fn a_known_end_is_read_without_asking_its_holder() {
        let ends: &'static Ends = Box::leak(Box::new(Ends::new()));
        ends.at.lock().unwrap().insert("h1".into(), 101_000);
        *ends.asked.lock().unwrap() = Some(Instant::now());
        static ASKED: AtomicBool = AtomicBool::new(false);
        let mirrors = BTreeMap::from([("a".to_string(), ended("h1", 100, SID))]);
        assert_eq!(ended_holders(&mirrors), BTreeSet::from(["h1".to_string()]));
        // The relay loop's tick, every second: nothing is asked while the answer is fresh.
        ends.refresh(ended_holders(&mirrors), PROBE_EVERY, |_: &str| { ASKED.store(true, SeqCst); None });
        assert_eq!(keys(&kept(&mirrors, &|id: &str| ends.get(id))), ["a"]);
        std::thread::sleep(Duration::from_millis(50));
        assert!(!ASKED.load(SeqCst) && !ends.asking.load(SeqCst), "a holder was asked on a warm cache");
        // No ended session names a holder: what was known is dropped, and still nothing is asked.
        asked_long_ago(ends);
        ends.refresh(BTreeSet::new(), PROBE_EVERY, |_: &str| { ASKED.store(true, SeqCst); None });
        assert_eq!(ends.get("h1"), None);
        assert!(!ASKED.load(SeqCst) && !ends.asking.load(SeqCst));
    }

    #[test]
    fn a_holder_that_never_answers_cannot_delay_a_tick() {
        let ends: &'static Ends = Box::leak(Box::new(Ends::new()));
        let holders = || BTreeSet::from(["h1".to_string()]);
        let (answer, wait) = std::sync::mpsc::channel::<()>();
        let wait = Mutex::new(wait);
        let started = Instant::now();
        ends.refresh(holders(), PROBE_EVERY, move |_: &str| { let _ = wait.lock().unwrap().recv(); None });
        assert_eq!(ends.get("h1"), None, "not known yet: no screen is kept, and nothing waits");
        // Later ticks, while that holder still has not answered: no second thread, no wait.
        static AGAIN: AtomicBool = AtomicBool::new(false);
        for _ in 0..3 {
            asked_long_ago(ends);
            ends.refresh(holders(), PROBE_EVERY, |_: &str| { AGAIN.store(true, SeqCst); None });
        }
        assert!(started.elapsed() < Duration::from_millis(200), "a tick waited {:?} for a holder", started.elapsed());
        assert!(!AGAIN.load(SeqCst), "a second pass started beside the stuck one");
        // It answers at last (or its read times out): the next pass runs and is read on a later tick.
        drop(answer);
        for _ in 0..200 {
            asked_long_ago(ends);
            ends.refresh(holders(), PROBE_EVERY, |_: &str| Some(5));
            if ends.get("h1") == Some(5) {
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        panic!("the pass after a stuck one never ran");
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

    fn holder(id: &str, pid: u32) -> HolderInfo {
        HolderInfo { id: id.into(), socket: format!("/x/{id}.sock").into(), pid, provider_pid: 2, argv: vec![], cwd: "/".into(), watch: true, type_input: true }
    }

    #[test]
    fn an_ended_holder_is_closed_unless_a_running_session_or_a_reported_screen_names_it() {
        let holders = [holder("h1", 10), holder("fresh", 11), holder("h3", 12), holder("livehold", 13), holder("resumed", 14)];
        let mut live = test_mirror("thread-live", "gen-1");
        live.native.holder = Some("livehold".into());
        let mirrors = BTreeMap::from([
            ("a".to_string(), ended("h1", 100, SID)),
            ("c".to_string(), ended("h3", 50, "left-earlier")),
            ("j".to_string(), live),
        ]);
        let end = |_: &str| Some(101_000);
        let named = named(&mirrors, &kept(&mirrors, &end));
        let unknown = unknown(&mirrors, &end);
        // `fresh` runs (no mirror names it yet); `h3` shows a screen no session may be shown;
        // `resumed` is a session's old holder, which no mirror names any more.
        let ended = |x: &HolderInfo| (x.id != "fresh").then_some(101_000);
        assert_eq!(orphans(&holders, &named, &unknown, ended, 101_000 + SWEEP_AFTER_MS), vec![12, 14]);
        assert!(orphans(&holders, &named, &unknown, ended, 101_000 + SWEEP_AFTER_MS - 1).is_empty(), "never in the moments after an end");
    }

    #[test]
    fn an_ended_holder_is_never_closed_while_its_screen_is_not_yet_known() {
        // Prod 2026-10-07 14:18Z, desktop 0.4.629: `/exit` in a held session, and its holder was
        // gone within 4 s. The end is read from a cache a background pass fills (`Ends`): right
        // after the exit it has no answer yet, and "not asked yet" read as "shows no session's screen".
        let holders = [holder("h1", 10)];
        let mirrors = BTreeMap::from([("a".to_string(), ended("h1", 100, SID))]);
        let cold = |_: &str| None;
        assert!(kept(&mirrors, &cold).is_empty(), "nothing is kept yet");
        let needed = named(&mirrors, &kept(&mirrors, &cold));
        // The sweep's own question, asked of the holder itself, is answered at once: it has ended.
        let ended = |_: &HolderInfo| Some(100_500);
        for later in [0, SWEEP_AFTER_MS, 24 * 3_600_000] {
            assert!(orphans(&holders, &needed, &unknown(&mirrors, &cold), ended, 100_500 + later).is_empty(), "unknown is not `no screen` ({later} ms)");
        }
        // The pass answers: the screen is kept, and its holder is named.
        let warm = |_: &str| Some(100_500);
        assert!(unknown(&mirrors, &warm).is_empty());
        assert!(named(&mirrors, &kept(&mirrors, &warm)).contains("h1"));
        assert_eq!(settling(&mirrors, &kept(&mirrors, &cold), 104_000), BTreeSet::from([SID.to_string()]), "a relay waits for the answer");
        assert!(settling(&mirrors, &kept(&mirrors, &warm), 104_000).is_empty(), "kept: nothing to wait for");
        assert!(settling(&mirrors, &kept(&mirrors, &cold), 100_000 + SETTLE_MS).is_empty(), "long ended: its holder ran on, or is gone");
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
        assert_eq!(m.native.terminal_reason(), Some("app"), "in an app: no terminal at all");
        assert!(!m.withheld(), "so a shared one is shared as its conversation");
        m.native.app = false;
        m.native.process.executable = "/opt/homebrew/bin/codex".into();
        assert_eq!(m.native.terminal_reason(), Some("no-daemon"));
        assert!(!m.withheld(), "shared, in a terminal this Mac cannot show: shared as its conversation (owner ruling 2026-10-11)");
        // The ChatGPT app's bundled CLI, run in a terminal: a terminal Codex like any other.
        m.native.process.executable = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex".into();
        assert_eq!(m.native.terminal_reason(), Some("no-daemon"));
        assert!(!m.withheld(), "shared as its conversation");
        m.native.provider = "claude".into();
        assert_eq!(m.native.terminal_reason(), Some("plain"));
        assert!(!m.withheld(), "a plain `claude` is shared as its conversation");
        m.native.job = Some("8ed71738".into());
        assert_eq!(m.native.terminal_reason(), None, "attach");
        assert!(!m.withheld(), "← made it live");
        m.shared = false;
        assert!(m.withheld(), "live, and only its person's");
        m.shared = true;
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
        m.native.app = false;
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
