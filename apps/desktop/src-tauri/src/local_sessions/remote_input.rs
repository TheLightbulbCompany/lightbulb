//! Remote typing (owner ruling 2026-10-03, Task 9). The backend forwards an admitted teammate's
//! keystrokes as `local.session.input`; this writes them to the session's holder as its one
//! `type` client. Never answered, whatever arrives. Dropped unless the session is in the live
//! relay set at that generation (someone watches: nobody types blind), the epoch is the relay's
//! current one, and the holder takes `type` clients. Terminal probe replies are stripped first.
//! Frames not marked `fromOwner` (a teammate's, or any from an older backend) also lose ctrl+x
//! and mouse reports: the session's Claude band takes keys only after ctrl+x Tab or a click, so
//! a teammate cannot operate it (spec 2026-10-04-session-sharing-mod §2.4). The owner typing in
//! the Lightbulb app (web, desktop, phone) comes through here too; the backend marks those
//! frames `fromOwner` and they pass untouched. The owner's own terminal never comes through here.
//! The connection task only queues: the holder lookup and the writes run on the relay's own
//! writer thread, which closes its `type` client when the relay stops.

use super::screen::{self, Ctl};
use crate::holder::protocol::{encode, read_frame, HELLO, INPUT, OWNER_INPUT, RESIZE};
use base64::Engine;
use serde_json::Value;
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::Ordering::SeqCst;
use std::sync::mpsc::{sync_channel, Receiver, RecvTimeoutError, TrySendError};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// The backend's per-call bound on raw bytes.
const MAX_INPUT: usize = 4096;
/// Calls queued for one holder; more are dropped (the backend already rate-bounds them).
const QUEUE: usize = 64;
const TICK: Duration = Duration::from_millis(50);
/// After a failed attach, inputs are dropped this long before trying again.
const RETRY: Duration = Duration::from_secs(1);

/// A band decision is refused this long after a teammate's bytes reached the session: the band
/// may already be focused by the owner, and `a`/`d`/`p` are its hotkeys (spec §2.4). The holder's
/// `lastInput` is the main check (`teammate_holds_decision`); this window is the second one, and
/// the only one for a holder too old to report `lastInput`.
pub(super) const TEAMMATE_QUIET: Duration = Duration::from_secs(2);
/// When a teammate's bytes last reached each session's holder.
static TEAMMATE_TYPED: Mutex<BTreeMap<String, Instant>> = Mutex::new(BTreeMap::new());

/// Records a write that reached the holder; only a teammate's counts.
fn wrote(sid: &str, from_owner: bool, now: Instant) {
    if from_owner {
        return;
    }
    if let Ok(mut map) = TEAMMATE_TYPED.lock() {
        map.retain(|_, at| now.saturating_duration_since(*at) < TEAMMATE_QUIET);
        map.insert(sid.to_owned(), now);
    }
}

/// A teammate who typed: (user id, display name), from the frame's `actor`.
pub(crate) type Typist = (String, String);
/// The teammate whose bytes last reached each session's holder.
// ponytail: one entry per session a teammate typed into, kept until the app quits (a few hundred
// bytes each). Prune with the mirror's end if that ever shows up in memory.
static LAST_TYPIST: Mutex<BTreeMap<String, (Typist, Instant)>> = Mutex::new(BTreeMap::new());
/// How long after a teammate's Enter the mod's question may still be about that prompt.
const SUBMIT_WINDOW: Duration = Duration::from_secs(3);

/// The frame's `actor`, bounded and printable. None from an older backend, or when the name
/// could not be read there.
fn typist_of(body: &Value) -> Option<Typist> {
    let user = body["actor"]["userId"].as_str().filter(|u| !u.is_empty() && u.len() <= 128)?;
    let name: String = body["actor"]["name"].as_str()?.chars().filter(|c| !c.is_control()).take(80).collect();
    (!name.trim().is_empty()).then(|| (user.to_owned(), name.trim().to_owned()))
}

/// Records the teammate whose Enter just reached the holder: the Enter submits the prompt, so a
/// frame without one changes nothing (another teammate typing after it is not the submitter).
/// An unnamed teammate's Enter clears the record: the submitter is then unknown, never the one
/// before. The owner's frames leave it alone (the holder says when the owner typed last).
fn typed(sid: &str, from_owner: bool, typist: Option<Typist>, keys: &[u8], now: Instant) {
    if from_owner || !keys.iter().any(|b| matches!(b, b'\r' | b'\n')) {
        return;
    }
    if let Ok(mut map) = LAST_TYPIST.lock() {
        match typist {
            Some(t) => map.insert(sid.to_owned(), (t, now)),
            None => map.remove(sid),
        };
    }
}

/// Whether a teammate's bytes ever reached `sid` under a name: the cheap check before the
/// blocking holder query.
pub(super) fn has_typist(sid: &str) -> bool {
    LAST_TYPIST.lock().is_ok_and(|m| m.contains_key(sid))
}

/// The teammate who submitted the session's last prompt, for the mod's "<Name> · typed" row.
/// Only when the holder says a teammate wrote last (`last_input`, from `holder_last_input`) and
/// that teammate's Enter is within `SUBMIT_WINDOW`. A holder too old to say names nobody: it
/// cannot see the owner's own keyboard, so a guess could put a teammate's name on the owner's
/// prompt.
// ponytail: the Enter is what counts. The owner types a prompt and a teammate presses Enter: it
// is labelled the teammate's. The owner presses Enter and a teammate types within the window
// before the mod asks: the holder says "teammate" and an earlier submitter may be named.
pub(super) fn typed_by(sid: &str, now: Instant, last_input: Option<&str>) -> Option<Typist> {
    if last_input != Some("teammate") {
        return None;
    }
    let (typist, at) = LAST_TYPIST.lock().ok()?.get(sid).cloned()?;
    (now.saturating_duration_since(at) < SUBMIT_WINDOW).then_some(typist)
}

/// Whether a teammate's bytes reached `sid` within `TEAMMATE_QUIET` of `now`.
pub(super) fn teammate_typed_recently(sid: &str, now: Instant) -> bool {
    TEAMMATE_TYPED.lock().ok().and_then(|m| m.get(sid).copied()).is_some_and(|at| now.saturating_duration_since(at) < TEAMMATE_QUIET)
}

/// Whether a band decision for `sid` is refused until the owner presses again: the holder says a
/// teammate wrote the last input (`last_input`, from `holder_last_input`), or a teammate's bytes
/// reached it within `TEAMMATE_QUIET`.
// ponytail: a teammate's `a` reaches the pty, Claude stalls, and the owner types another key
// before Claude reads the `a`: `lastInput` is then the owner's and the decision is the owner's.
// Closing that needs Claude to say which key it acted on.
pub(super) fn teammate_holds_decision(sid: &str, now: Instant, last_input: Option<&str>) -> bool {
    last_input == Some("teammate") || teammate_typed_recently(sid, now)
}

/// Who wrote the last input to the holder hosting `sid`'s terminal: `"owner"` or `"teammate"`.
/// None when no holder hosts it (a transcript, or a background job: the window decides), nothing
/// was typed yet, or the holder is too old to say. Blocking (≤ 2 s).
pub(super) fn holder_last_input(sid: &str) -> Option<String> {
    last_input_of(&screen::hosting_holder(sid)?.socket)
}

fn last_input_of(socket: &Path) -> Option<String> {
    let mut conn = UnixStream::connect(socket).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.set_write_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.write_all(&encode(HELLO, br#"{"role":"origin"}"#)).ok()?;
    let (kind, facts) = read_frame(&mut conn).ok()?;
    let facts: Value = serde_json::from_slice(&facts).ok()?;
    (kind == HELLO).then(|| facts["lastInput"].as_str().map(str::to_owned)).flatten()
}

/// How long every Lightbulb view shows a teammate's colour and name on the cursor after their keys (S8).
pub(crate) const CURSOR_FOR: Duration = Duration::from_secs(4);
/// The teammate whose keys last reached each holder (by holder id), for the app panel's cursor.
/// The owner's keys, and an unnamed teammate's, clear it.
static CURSOR: Mutex<BTreeMap<String, (Typist, Instant)>> = Mutex::new(BTreeMap::new());

fn cursor_typed(holder: &str, from_owner: bool, typist: Option<Typist>, now: Instant) {
    if let Ok(mut map) = CURSOR.lock() {
        match typist.filter(|_| !from_owner) {
            Some(t) => map.insert(holder.to_owned(), (t, now)),
            None => map.remove(holder),
        };
    }
}

/// The owner typed into `holder` from the app panel: the plain cursor, at once.
pub(crate) fn owner_typed(holder: &str) {
    cursor_typed(holder, true, None, Instant::now());
}

/// Who the panel's cursor names now, and since when; None after `CURSOR_FOR`.
pub(crate) fn cursor_typist(holder: &str, now: Instant) -> Option<(Typist, Instant)> {
    CURSOR.lock().ok()?.get(holder).filter(|(_, at)| now.saturating_duration_since(*at) < CURSOR_FOR).cloned()
}

/// The app's panel size: (cols, rows, whether the panel claims the size; it takes it only with keys).
pub(super) type PanelSize = (u16, u16, bool);
/// Keys for the holder, whether the session's owner typed them, the teammate who did, and the
/// panel's size when the frame carries one (owner ruling 2026-10-14: the session takes the size of
/// whoever used it last, tmux's `window-size latest`; the holder keeps that rule, `run::latest_user`).
pub(super) type Typed = (Vec<u8>, bool, Option<Typist>, Option<PanelSize>);

/// A `local.session.input` frame from the relay. Never blocks, never replies.
pub(super) fn offer(body: &Value) {
    let Some((sid, generation, ctl, mut keys, size)) = admit(body, screen::live_relay) else { return };
    let from_owner = body["fromOwner"] == true;
    let typist = typist_of(body);
    let Ok(mut slot) = ctl.typist.lock() else { return };
    if let Some(tx) = slot.as_ref() {
        match tx.try_send((keys, from_owner, typist.clone(), size)) {
            Ok(()) | Err(TrySendError::Full(_)) => return,
            // Its writer is gone: start another.
            Err(TrySendError::Disconnected((back, ..))) => keys = back,
        }
    }
    let (tx, rx) = sync_channel(QUEUE);
    let _ = tx.try_send((keys, from_owner, typist, size));
    *slot = Some(tx);
    let theirs = ctl.clone();
    std::thread::spawn(move || write(&sid, &generation, &theirs, &rx));
}

/// The frame's `size`, when it is one a terminal can be.
fn panel_size(body: &Value) -> Option<PanelSize> {
    let size = &body["size"];
    let cols = size["cols"].as_u64().filter(|c| (1..=1000).contains(c))?;
    let rows = size["rows"].as_u64().filter(|r| (1..=500).contains(r))?;
    Some((cols as u16, rows as u16, size["claim"] == true))
}

/// (session id, generation, its relay, the keys to write, the panel's size) when the frame may
/// reach the holder: keys, a size, or both.
fn admit(body: &Value, relay_of: impl Fn(&Value) -> Option<(String, Arc<Ctl>)>) -> Option<(String, String, Arc<Ctl>, Vec<u8>, Option<PanelSize>)> {
    let data = body["data"].as_str().filter(|d| d.len() <= MAX_INPUT.div_ceil(3) * 4)?;
    let epoch = body["epoch"].as_u64().filter(|e| *e != 0)?;
    let generation = body["generation"].as_str()?.to_owned();
    let (sid, ctl) = relay_of(body)?;
    if ctl.epoch.load(SeqCst) != epoch {
        return None;
    }
    let raw = base64::engine::general_purpose::STANDARD.decode(data).ok().filter(|r| r.len() <= MAX_INPUT)?;
    // A missing `fromOwner` (an older backend) strips: the safe default.
    let keys = strip_remote(&raw, body["fromOwner"] != true);
    let size = panel_size(body);
    (!keys.is_empty() || size.is_some()).then_some((sid, generation, ctl, keys, size))
}

/// The relay's writer: one `type` client, reattached after an error, closed when the relay stops.
fn write(sid: &str, generation: &str, ctl: &Ctl, inputs: &Receiver<Typed>) {
    write_with(sid, generation, ctl, inputs, attach)
}

fn write_with(sid: &str, generation: &str, ctl: &Ctl, inputs: &Receiver<Typed>, attach: impl Fn(&str, &str) -> Option<(UnixStream, bool, String)>) {
    // The `type` client, whether its holder takes OWNER_INPUT, and the holder's id.
    let mut conn: Option<(UnixStream, bool, String)> = None;
    let mut tried: Option<Instant> = None;
    let mut carry = Carry::default();
    while !ctl.stopped() {
        let (keys, from_owner, typist, size) = match inputs.recv_timeout(TICK) {
            // A size alone feeds nothing: a held escape stays held for the next keys.
            Ok((keys, from_owner, typist, size)) => (if keys.is_empty() { keys } else { carry.feed(&keys, from_owner) }, from_owner, typist, size),
            Err(RecvTimeoutError::Timeout) => continue,
            Err(RecvTimeoutError::Disconnected) => break,
        };
        if keys.is_empty() && size.is_none() {
            continue;
        }
        for _ in 0..2 {
            if conn.is_none() && tried.is_none_or(|at| at.elapsed() >= RETRY) {
                tried = Some(Instant::now());
                conn = attach(sid, generation);
            }
            let Some((c, origin, holder)) = conn.as_mut() else { break };
            // Sharing may have stopped since these keys were taken off the queue (the attach
            // above can wait two seconds): the last moment before they are recorded and written.
            if ctl.stopped() {
                break;
            }
            // The size first: keys typed in the panel are drawn at the panel's size. An older
            // holder ignores a `type` client's RESIZE. Only typing takes the size (owner ruling
            // 2026-10-27): a page that opens the session and only watches leaves the owner's
            // terminal at its size, and adapts to it.
            let resized = size.is_none_or(|(cols, rows, claim)| {
                let mut payload = cols.to_be_bytes().to_vec();
                payload.extend_from_slice(&rows.to_be_bytes());
                payload.push((claim && !keys.is_empty()) as u8);
                c.write_all(&encode(RESIZE, &payload)).is_ok()
            });
            let typed_ok = resized && (keys.is_empty() || {
                // Recorded first: a decide that reads it between the write and the record is refused.
                wrote(sid, from_owner, Instant::now());
                typed(sid, from_owner, typist.clone(), &keys, Instant::now());
                cursor_typed(holder, from_owner, typist.clone(), Instant::now());
                // An older holder takes only INPUT, and reports no origin to mark.
                let kind = if from_owner && *origin { OWNER_INPUT } else { INPUT };
                c.write_all(&encode(kind, &keys)).is_ok()
            });
            if typed_ok {
                break;
            }
            // The holder went away or restarted: reattach once, now.
            conn = None;
            tried = None;
        }
    }
    if let Some((c, ..)) = conn {
        let _ = c.shutdown(std::net::Shutdown::Both);
    }
}

/// A `type` client on the session's holder, only when its HELLO says it takes one, whether it
/// takes OWNER_INPUT, and the holder's id.
fn attach(sid: &str, generation: &str) -> Option<(UnixStream, bool, String)> {
    let (holder, current) = screen::holder_for_session(sid, false)?;
    if current != generation || !holder.type_input {
        return None;
    }
    let mut conn = UnixStream::connect(&holder.socket).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.set_write_timeout(Some(Duration::from_secs(2))).ok()?;
    conn.write_all(&encode(HELLO, br#"{"role":"type"}"#)).ok()?;
    let (kind, facts) = read_frame(&mut conn).ok()?;
    let facts: Value = serde_json::from_slice(&facts).ok()?;
    (kind == HELLO && facts["type"] == true).then_some((conn, facts["origin"] == true, holder.id))
}

/// Remote bytes minus probe replies and, with `band`, band keys, until nothing more goes:
/// removing one sequence can join its neighbours into another (`ESC ^X [120;5u`).
pub(super) fn strip_remote(input: &[u8], band: bool) -> Vec<u8> {
    let mut cur = strip_pass(input, band);
    loop {
        let next = strip_pass(&cur, band);
        if next.len() == cur.len() {
            return next;
        }
        cur = next;
    }
}

/// What a teammate's frames may leave unfinished on the pty. A frame is the client's to cut, so
/// `ESC [120;5` then `u` must not add up to ctrl+x there: an unfinished escape is held until
/// the next frame completes it, except a lone ESC (the Escape key), which is written at once.
/// An unfinished escape already written (that ESC, or the owner's) is remembered, and a
/// teammate's frame that would finish it into a band key is dropped.
#[derive(Default)]
struct Carry {
    written: Vec<u8>,
    held: Vec<u8>,
}

impl Carry {
    fn feed(&mut self, keys: &[u8], from_owner: bool) -> Vec<u8> {
        if from_owner {
            // The owner's keys go out as typed; a teammate's held fragment is dropped.
            let mut ctx = std::mem::take(&mut self.written);
            ctx.extend_from_slice(keys);
            self.held.clear();
            self.written = ctx.split_off(unfinished_from(&ctx));
            if self.written.len() > 64 {
                self.written.clear();
            }
            return keys.to_vec();
        }
        let w = self.written.len();
        let mut buf = self.written.clone();
        buf.append(&mut self.held);
        buf.extend_from_slice(keys);
        let cut = unfinished_from(&buf);
        if cut < w {
            // Nothing complete yet. What is written stays written; the rest waits (bounded).
            self.held = buf.split_off(w);
            if self.held.len() > 64 {
                self.held.clear();
            }
            return vec![];
        }
        let mut out = strip_remote(&buf[..cut], true);
        if w > 0 {
            if out.starts_with(&self.written) && out[w..] == strip_remote(&buf[w..cut], true)[..] {
                out.drain(..w); // the written part is inert: write only what follows it
            } else {
                out.clear(); // it joined a removed sequence: the rest could finish one on the pty
            }
            self.written.clear();
        }
        // What is left unwritten: an unfinished escape stripping exposed, then the frame's own.
        let mut unwritten = out.split_off(unfinished_from(&out));
        unwritten.extend_from_slice(&buf[cut..]);
        if unwritten == [0x1b] {
            out.push(0x1b);
            self.written = unwritten;
        } else if unwritten.len() <= 64 {
            self.held = unwritten;
        }
        out
    }
}

/// Where a trailing unfinished escape starts (`ESC`, `ESC [` with only parameter bytes, or an
/// `ESC [ M` mouse report short of its 3 bytes), or the length when there is none.
fn unfinished_from(buf: &[u8]) -> usize {
    let Some(e) = buf.iter().rposition(|b| *b == 0x1b) else { return buf.len() };
    let unfinished = match buf.get(e + 1) {
        None => true,
        Some(b'[') => match buf.get(e + 2) {
            Some(b'M') => buf.len() < e + 6,
            _ => buf[e + 2..].iter().all(|b| (0x20..=0x3f).contains(b)),
        },
        _ => false,
    };
    if unfinished {
        e
    } else {
        buf.len()
    }
}

/// One pass: the bytes minus the replies a terminal (xterm.js) sends to queries (DA, CPR, DSR,
/// DECRPM, kitty keyboard flags, focus, OSC 4/10/11/12 colours, XTVERSION and DECRQSS: the
/// holder answers queries itself, so a remote viewer's answers would reach the provider as
/// typing) and minus band keys (ctrl+x in any encoding, every mouse report).
/// Known cost: Shift/Ctrl+F3 (`ESC [ 1 ; n R`) reads as a CPR and is dropped.
fn strip_pass(input: &[u8], band: bool) -> Vec<u8> {
    let mut out = Vec::with_capacity(input.len());
    let mut i = 0;
    while i < input.len() {
        let cut = match (input[i], input.get(i + 1)) {
            (0x18, _) if band => Some(1), // legacy ctrl+x
            (0x1b, Some(b'[')) => csi_reply(&input[i + 2..]).or_else(|| band_csi(&input[i + 2..]).filter(|_| band)).map(|n| n + 2),
            (0x1b, Some(&kind @ (b']' | b'P'))) => string_reply(kind, &input[i + 2..]).map(|n| n + 2),
            _ => None,
        };
        match cut {
            Some(len) => i += len,
            None => {
                out.push(input[i]);
                i += 1;
            }
        }
    }
    out
}

/// The length of a CSI after `ESC [` when it is a query reply; None for a key (or incomplete).
fn csi_reply(rest: &[u8]) -> Option<usize> {
    let params = rest.iter().take_while(|b| (0x30..=0x3f).contains(*b)).count();
    let inter = rest[params..].iter().take_while(|b| (0x20..=0x2f).contains(*b)).count();
    let last = *rest.get(params + inter).filter(|b| (0x40..=0x7e).contains(*b))?;
    let (p, i) = (&rest[..params], &rest[params..params + inter]);
    let numeric = |s: &[u8]| !s.is_empty() && s.iter().all(|b| b.is_ascii_digit() || *b == b';');
    let reply = match (last, i) {
        (b'c', []) => matches!(p.first(), Some(b'?' | b'>')),
        (b'R', []) => p.contains(&b';') && numeric(p.strip_prefix(b"?".as_slice()).unwrap_or(p)),
        (b'n', []) => p == b"0",
        (b'y', [b'$']) => true,
        (b'u', []) => p.first() == Some(&b'?'),
        (b'I' | b'O', []) => p.is_empty(),
        _ => false,
    };
    reply.then_some(params + inter + 1)
}

/// The length of a CSI after `ESC [` when it operates the band: a mouse report (SGR
/// `<b;x;y M|m`, urxvt `b;x;y M`, X10 `M` and 3 bytes) or ctrl+x as CSI-u (`120;5u`, kitty
/// `120:88;5:1u`, also `X` and code 24) or modifyOtherKeys (`27;5;120~`). None otherwise.
fn band_csi(rest: &[u8]) -> Option<usize> {
    if rest.first() == Some(&b'M') {
        // Unfinished until its 3 bytes are here: `Carry` holds it, and strips it whole.
        return (rest.len() >= 4).then_some(4);
    }
    let params = rest.iter().take_while(|b| (0x30..=0x3f).contains(*b)).count();
    let inter = rest[params..].iter().take_while(|b| (0x20..=0x2f).contains(*b)).count();
    let last = *rest.get(params + inter).filter(|b| (0x40..=0x7e).contains(*b))?;
    let p = &rest[..params];
    let fields: Vec<&[u8]> = p.split(|b| *b == b';').collect();
    // A field's first `:` part as a number; an empty one is `default`.
    let num = |f: Option<&&[u8]>, default: u32| -> Option<u32> {
        let head = f.map_or(&[][..], |f| f.split(|b| *b == b':').next().unwrap_or(&[]));
        if head.is_empty() {
            return Some(default);
        }
        std::str::from_utf8(head).ok()?.parse().ok()
    };
    let ctrl_x = |key: Option<u32>, mods: Option<u32>| matches!(key, Some(24 | 88 | 120)) && mods.is_some_and(|m| m >= 1 && (m - 1) & 4 != 0);
    let digits = |f: &&[u8]| !f.is_empty() && f.iter().all(u8::is_ascii_digit);
    let band = inter == 0
        && match last {
            b'M' | b'm' if p.first() == Some(&b'<') => true,
            b'M' => fields.len() == 3 && fields.iter().all(digits),
            b'u' => p.first().is_some_and(u8::is_ascii_digit) && ctrl_x(num(fields.first(), 0), num(fields.get(1), 1)),
            b'~' => fields.first() == Some(&&b"27"[..]) && ctrl_x(num(fields.get(2), 0), num(fields.get(1), 1)),
            _ => false,
        };
    band.then_some(params + inter + 1)
}

/// The length of an OSC (`]`) or DCS (`P`) after its introducer, through BEL or `ESC \`, when it
/// is a query reply; None otherwise or when it is not terminated here.
fn string_reply(kind: u8, rest: &[u8]) -> Option<usize> {
    let end = rest.iter().position(|b| *b == 0x07 || *b == 0x1b)?;
    let term = match rest[end] {
        0x07 => 1,
        _ if rest.get(end + 1) == Some(&b'\\') => 2,
        _ => return None,
    };
    let replies: &[&[u8]] = if kind == b']' { &[b"4;", b"10;", b"11;", b"12;"] } else { &[b">|", b"1$r", b"0$r"] };
    replies.iter().any(|r| rest[..end].starts_with(r)).then_some(end + term)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_already_taken_off_the_queue_are_not_written_once_the_relay_is_stopped() {
        use std::io::Read;
        let (holder, mut peer) = UnixStream::pair().unwrap();
        peer.set_read_timeout(Some(Duration::from_millis(200))).unwrap();
        let ctl = Ctl::new(0);
        let (tx, rx) = sync_channel(QUEUE);
        tx.send((b"rm -rf".to_vec(), true, None, None)).unwrap();
        drop(tx); // nothing more comes: the writer ends even if it never attaches
        // Sharing is stopped while the writer attaches to the holder.
        let holder = std::cell::RefCell::new(Some(holder));
        write_with("sid", "gen", &ctl, &rx, |_, _| {
            ctl.stop.store(true, SeqCst);
            holder.borrow_mut().take().map(|conn| (conn, true, "h".to_owned()))
        });
        let mut read = [0u8; 16];
        assert!(!matches!(peer.read(&mut read), Ok(n) if n > 0), "nothing reached the holder");
    }
    use serde_json::json;
    use std::collections::BTreeMap;

    #[test]
    fn remote_input_the_panel_cursor_names_a_teammate_for_four_seconds_and_the_owner_clears_it() {
        let (h, now) = ("holder-cursor", Instant::now());
        let aden: Typist = ("user_a".into(), "Aden".into());
        cursor_typed(h, false, Some(aden.clone()), now);
        assert_eq!(cursor_typist(h, now + Duration::from_secs(3)), Some((aden.clone(), now)));
        assert_eq!(cursor_typist(h, now + CURSOR_FOR), None, "plain again after four seconds");
        cursor_typed(h, false, Some(aden), now);
        owner_typed(h);
        assert_eq!(cursor_typist(h, now), None, "the owner's keys clear it at once");
    }

    #[test]
    fn remote_input_a_teammate_write_holds_band_decisions_for_two_seconds() {
        let (sid, owner_sid) = ("55555555-5555-4555-8555-555555555555", "66666666-6666-4666-8666-666666666666");
        let now = Instant::now();
        assert!(!teammate_typed_recently(sid, now));
        wrote(sid, false, now);
        assert!(teammate_typed_recently(sid, now), "a decide right after a teammate's write is refused");
        assert!(teammate_typed_recently(sid, now + TEAMMATE_QUIET - Duration::from_millis(1)));
        assert!(!teammate_typed_recently(sid, now + TEAMMATE_QUIET), "allowed once the window passes");
        wrote(owner_sid, true, now);
        assert!(!teammate_typed_recently(owner_sid, now), "the owner's frames never hold a decision");
    }

    #[test]
    fn remote_input_a_decision_waits_when_the_holder_says_a_teammate_typed_last() {
        let sid = "77777777-7777-4777-8777-777777777777";
        let now = Instant::now();
        assert!(teammate_holds_decision(sid, now, Some("teammate")), "however long ago: origin, not time");
        assert!(!teammate_holds_decision(sid, now, Some("owner")));
        assert!(!teammate_holds_decision(sid, now, None), "no holder, or nothing typed: allowed");
        wrote(sid, false, now);
        assert!(teammate_holds_decision(sid, now, Some("owner")), "the 2 s window is the second check");
        assert!(teammate_holds_decision(sid, now, None), "an older holder: the window alone");
        assert!(!teammate_holds_decision(sid, now + TEAMMATE_QUIET, None));
    }

    #[test]
    fn remote_input_names_the_teammate_only_while_their_input_is_the_last() {
        let sid = "88888888-8888-4888-8888-888888888888";
        let now = Instant::now();
        let aden = typist_of(&json!({"actor": {"userId": "user_a", "name": " Aden\u{1b}[31m Kim\n"}})).unwrap();
        assert_eq!(aden, ("user_a".to_owned(), "Aden[31m Kim".to_owned()), "control characters never reach the terminal");
        assert!(typist_of(&json!({"fromOwner": false})).is_none(), "an older backend names nobody");
        assert!(typist_of(&json!({"actor": {"userId": "", "name": "x"}})).is_none());
        assert!(typist_of(&json!({"actor": {"userId": "u", "name": " "}})).is_none());
        assert!(!has_typist(sid));
        assert_eq!(typed_by(sid, now, Some("teammate")), None, "a teammate typed, but nobody said who");
        typed(sid, false, Some(aden.clone()), b"hello", now);
        assert!(!has_typist(sid), "typing without Enter submits nothing");
        typed(sid, false, Some(aden.clone()), b"hello\r", now);
        assert!(has_typist(sid));
        assert_eq!(typed_by(sid, now, Some("teammate")), Some(aden.clone()));
        assert_eq!(typed_by(sid, now, Some("owner")), None, "the owner typed last: their own prompt");
        wrote(sid, false, now);
        assert_eq!(typed_by(sid, now, None), None, "an older holder cannot see the owner's keys: nobody is named");
        let mira = ("user_m".to_owned(), "Mira".to_owned());
        typed(sid, false, Some(mira), b"x", now);
        assert_eq!(typed_by(sid, now, Some("teammate")), Some(aden.clone()), "another teammate's keys after the Enter are not the submitter");
        typed(sid, true, None, b"\r", now);
        assert_eq!(typed_by(sid, now, Some("teammate")), Some(aden), "the owner's frames leave the record");
        assert_eq!(typed_by(sid, now + SUBMIT_WINDOW, Some("teammate")), None, "an old Enter is not this prompt's");
        typed(sid, false, None, b"\r", now);
        assert_eq!(typed_by(sid, now, Some("teammate")), None, "an unnamed teammate is never the one before");
    }

    #[test]
    fn remote_input_reads_last_input_from_a_holder_and_nothing_from_an_older_one() {
        use std::os::unix::net::UnixListener;
        let dir = tempfile::tempdir().unwrap();
        // A holder that answers any HELLO with `facts`.
        let holder = |name: &str, facts: Value| {
            let path = dir.path().join(name);
            let listener = UnixListener::bind(&path).unwrap();
            std::thread::spawn(move || {
                let (mut c, _) = listener.accept().unwrap();
                let (kind, hello) = read_frame(&mut c).unwrap();
                assert_eq!((kind, serde_json::from_slice::<Value>(&hello).unwrap()["role"].clone()), (HELLO, json!("origin")));
                c.write_all(&encode(HELLO, facts.to_string().as_bytes())).unwrap();
            });
            path
        };
        let new = holder("new.sock", json!({"type": true, "origin": true, "lastInput": "teammate"}));
        assert_eq!(last_input_of(&new).as_deref(), Some("teammate"));
        let fresh = holder("fresh.sock", json!({"type": true, "origin": true, "lastInput": null}));
        assert_eq!(last_input_of(&fresh), None, "nothing typed yet");
        let old = holder("old.sock", json!({"type": true}));
        assert_eq!(last_input_of(&old), None, "an older holder says nothing: the window decides");
        assert_eq!(last_input_of(&dir.path().join("gone.sock")), None);
    }

    #[test]
    fn remote_input_strips_terminal_probe_replies_and_keeps_typing() {
        let table: &[(&[u8], &[u8])] = &[
            (b"\x1b[?62;22c", b""),
            (b"a\x1b[>0;276;0cb", b"ab"),
            (b"\x1b[12;40R", b""),
            (b"\x1b[?12;40R", b""),
            (b"\x1b[0n", b""),
            (b"\x1b[?2004;1$y\x1b[4;2$y", b""),
            (b"\x1b[?1u", b""),
            (b"\x1b[I\x1b[O", b""),
            (b"\x1b]11;rgb:1616/1616/1a1a\x1b\\", b""),
            (b"\x1b]10;rgb:0/0/0\x07x", b"x"),
            (b"\x1b]4;1;rgb:cdcd/0000/0000\x07", b""),
            (b"\x1bP>|xterm.js(5.5.0)\x1b\\", b""),
            (b"h\x1b[?1;2ci", b"hi"),
            // Typing, kept as is.
            (b"ls -la\r", b"ls -la\r"),
            (b"\x1b[A\x1b[1;5D\x1bOP", b"\x1b[A\x1b[1;5D\x1bOP"),
            (b"\x1b[200~paste\x1b[201~", b"\x1b[200~paste\x1b[201~"),
            (b"\x1b\r", b"\x1b\r"),
            (b"\x1b[", b"\x1b["),
            (b"\x1b]", b"\x1b]"),
            (b"\x1b]11;rgb:0/0/0", b"\x1b]11;rgb:0/0/0"),
            (b"\x1b[5n", b"\x1b[5n"),
            ("caf\u{e9} \u{1f600}".as_bytes(), "caf\u{e9} \u{1f600}".as_bytes()),
        ];
        for (input, kept) in table {
            assert_eq!(strip_remote(input, true), *kept, "{input:?}");
        }
    }

    #[test]
    fn remote_input_strips_ctrl_x_and_mouse_reports_in_every_encoding() {
        let gone: &[&[u8]] = &[
            b"\x18",
            b"\x1b[120;5u",
            b"\x1b[88;5u",
            b"\x1b[120;6u",
            b"\x1b[120:88;6:1u",
            b"\x1b[120;5:3u",
            b"\x1b[120;13u",
            b"\x1b[24;5u",
            b"\x1b[27;5;120~",
            b"\x1b[27;5;88~",
            b"\x1b[<0;10;5M",
            b"\x1b[<0;10;5m",
            b"\x1b[<64;3;4M",
            b"\x1b[M !!",
            b"\x1b[32;10;5M",
            b"\x1b\x18[120;5u", // removing ^X would join ESC to a CSI-u ctrl+x
            b"\x1b[\x1b[<0;1;1M120;5u",
        ];
        for input in gone {
            assert_eq!(strip_remote(input, true), b"", "{input:?}");
            assert_eq!(strip_remote(input, false), *input, "the owner's keys pass: {input:?}");
        }
        let kept: &[(&[u8], &[u8])] = &[
            (b"ls -la\r", b"ls -la\r"),
            (b"\x03", b"\x03"),                         // ctrl+c
            (b"\x1b[A\x1b[B\x1b[1;5D", b"\x1b[A\x1b[B\x1b[1;5D"), // arrows
            (b"\x1b[99;5u\x1b[13u", b"\x1b[99;5u\x1b[13u"),       // kitty ctrl+c, Enter
            (b"\x1b[120u\x1b[120;2u", b"\x1b[120u\x1b[120;2u"),   // x and X without ctrl
            (b"\x1b[27;2;120~", b"\x1b[27;2;120~"),
            (b"\x1b\t", b"\x1b\t"),
            (b"a\x18\tb", b"a\tb"), // ctrl+x Tab loses its ctrl+x
            (b"x\x1b[<0;1;1My", b"xy"),
        ];
        for (input, out) in kept {
            assert_eq!(strip_remote(input, true), *out, "{input:?}");
        }
    }

    #[test]
    fn remote_input_a_band_key_split_across_frames_never_reaches_the_pty() {
        let run = |frames: &[&[u8]]| {
            let mut carry = Carry::default();
            frames.iter().flat_map(|f| carry.feed(f, false)).collect::<Vec<u8>>()
        };
        for frames in [
            &[&b"\x1b[120;5"[..], b"u"][..],
            &[b"\x1b[", b"120;5u"],
            &[b"\x1b", b"[120;5u"],
            &[b"\x1b", b"[", b"120;5u"],
            &[b"\x1b", b"[120;5u[120;5u"],
            &[b"a\x1b\x18", b"[120;5u"],
            &[b"\x1b[<0;10;", b"5M"],
            &[b"\x1b[M", b" !", b"!"],
            &[b"\x1b[27;5;12", b"0~"],
        ] {
            let pty = run(frames);
            assert!(strip_remote(&pty, true).len() == pty.len() && !pty.contains(&0x18), "{frames:?} wrote {pty:?}");
            assert!(pty.iter().all(|b| *b == 0x1b || *b == b'a'), "{frames:?} wrote {pty:?}");
        }
        // As `admit` sends them: each frame stripped on its own first, then carried.
        let admitted = |frames: &[&[u8]]| {
            let mut carry = Carry::default();
            frames.iter().flat_map(|f| carry.feed(&strip_remote(f, true), false)).collect::<Vec<u8>>()
        };
        for frames in [
            &[&b"\x1b[M"[..], b" !!"][..],
            &[b"\x1b[M ", b"!!"],
            &[b"\x1b[M !", b"!"],
            &[b"ab\x1b[M", b" !!cd"],
            &[b"\x1b[", b"M !!"],
            &[b"\x1b", b"[M !!"],
            &[b"\x1b[<0;1", b";1M"],
            &[b"\x1b[120;5", b"u"],
            &[b"\x1b[27;5;1", b"20~"],
        ] {
            let pty = admitted(frames);
            assert!(!pty.windows(2).any(|w| w == b" !") && strip_remote(&pty, true) == pty && !pty.contains(&0x18), "{frames:?} wrote {pty:?}");
            assert!(pty.iter().all(|b| b"\x1babcd".contains(b)), "{frames:?} wrote {pty:?}");
        }
        assert_eq!(admitted(&[b"ab\x1b[M", b" !!cd"]), b"abcd", "a split X10 report goes whole; the typing around it stays");
        assert_eq!(strip_remote(b"\x1b[M !", true), b"\x1b[M !", "an unfinished X10 report is left for Carry");
        assert_eq!(run(&[b"\x1b", b"[A"]), b"\x1b[A", "Escape, then an arrow");
        assert_eq!(run(&[b"\x1b[20", b"0~hi\x1b[201~"]), b"\x1b[200~hi\x1b[201~", "a paste cut mid-sequence");
        assert_eq!(run(&[b"\x1b"]), b"\x1b", "Escape goes out at once");
        assert_eq!(run(&[b"ls\r"]), b"ls\r");
        // The owner's unfinished escape cannot be finished by a teammate.
        let mut carry = Carry::default();
        assert_eq!(carry.feed(b"\x1b[120;5", true), b"\x1b[120;5");
        assert_eq!(carry.feed(b"u", false), b"");
        assert_eq!(carry.feed(b"\x1b", true), b"\x1b");
        assert_eq!(carry.feed(b"[A", false), b"[A", "an inert continuation goes out");
        assert_eq!(carry.feed(b"\x18\x1b[<0;1;1M", true), b"\x18\x1b[<0;1;1M", "the owner's band keys pass");
    }

    const SID: &str = "aaaabbbb-cccc-4ddd-8eee-ffff00001111";

    fn frame(sid: &str, generation: &str, epoch: u64, data: &[u8]) -> Value {
        json!({"type": "local.session.input", "sessionId": sid, "generation": generation, "epoch": epoch,
            "data": base64::engine::general_purpose::STANDARD.encode(data)})
    }

    #[test]
    fn remote_input_is_dropped_unless_watched_at_this_generation_and_the_current_epoch() {
        let _g = super::super::presence::TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let ctl = Arc::new(Ctl::new(0));
        ctl.epoch.store(7, SeqCst);
        let live = BTreeMap::from([(SID.to_owned(), "gen-1".to_owned())]);
        // `screen::live_relay` with the live mirrors and running relays given.
        let relay = |relays: BTreeMap<String, Arc<Ctl>>| {
            let live = live.clone();
            move |body: &Value| {
                let sid = screen::nudged(body, |s| live.get(s).cloned())?;
                Some((sid.clone(), relays.get(&sid)?.clone()))
            }
        };
        let watched = || relay(BTreeMap::from([(SID.to_owned(), ctl.clone())]));
        let (sid, generation, _, keys, size) = admit(&frame(SID, "gen-1", 7, b"ls\r\x1b[?62;22c"), watched()).unwrap();
        assert_eq!(size, None, "no size: keys only, as before");
        assert_eq!((sid.as_str(), generation.as_str(), keys.as_slice()), (SID, "gen-1", b"ls\r".as_slice()));
        assert!(admit(&frame(SID, "gen-1", 7, b"ls\r"), relay(BTreeMap::new())).is_none(), "nobody watches it");
        assert!(admit(&frame(SID, "gen-0", 7, b"ls\r"), watched()).is_none(), "another generation");
        assert!(admit(&frame(SID, "gen-1", 6, b"ls\r"), watched()).is_none(), "a stale epoch");
        assert!(admit(&frame("bbbbcccc-dddd-4eee-8fff-000011112222", "gen-1", 7, b"ls\r"), watched()).is_none(), "not this Mac's");
        assert!(admit(&frame(SID, "gen-1", 7, &[b'x'; MAX_INPUT + 1]), watched()).is_none(), "over the bound");
        assert!(admit(&frame(SID, "gen-1", 7, b"\x1b[12;40R"), watched()).is_none(), "nothing left to type");
        assert!(admit(&frame(SID, "gen-1", 7, b"\x18\x1b[<0;3;4M"), watched()).is_none(), "only band keys: dropped");
        let mut owner = frame(SID, "gen-1", 7, b"\x18\x1b[<0;3;4M");
        owner["fromOwner"] = json!(true);
        assert_eq!(admit(&owner, watched()).unwrap().3, b"\x18\x1b[<0;3;4M", "the owner's ctrl+x and clicks pass");
        owner["fromOwner"] = json!(false);
        assert!(admit(&owner, watched()).is_none(), "a teammate's do not");
        assert!(admit(&json!({"type": "local.session.input", "sessionId": SID, "generation": "gen-1", "epoch": 7, "data": "%%"}), watched()).is_none());
        // The app's panel size (owner ruling 2026-10-14): alone or with keys, never out of range.
        let mut sized = frame(SID, "gen-1", 7, b"");
        sized["size"] = json!({"cols": 120, "rows": 40, "claim": true});
        assert_eq!(admit(&sized, watched()).unwrap().4, Some((120, 40, true)), "a size alone reaches the holder");
        sized["size"] = json!({"cols": 120, "rows": 40});
        assert_eq!(admit(&sized, watched()).unwrap().4, Some((120, 40, false)), "no claim: a resize only");
        sized["size"] = json!({"cols": 0, "rows": 40, "claim": true});
        assert!(admit(&sized, watched()).is_none(), "a size no terminal can be: nothing to send");
        sized["epoch"] = json!(6);
        sized["size"] = json!({"cols": 120, "rows": 40, "claim": true});
        assert!(admit(&sized, watched()).is_none(), "a size from a stale epoch is dropped as keys are");
        ctl.epoch.store(0, SeqCst);
        assert!(admit(&frame(SID, "gen-1", 0, b"ls\r"), watched()).is_none(), "a relay that has not started streaming");
    }

    #[test]
    fn remote_input_is_never_dropped_because_a_request_is_pending() {
        use super::super::presence::{set, Entry, JoinRequest, TEST_LOCK};
        let _g = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let sid = "cccc1111-2222-4333-8444-555566667777";
        let ctl = Arc::new(Ctl::new(0));
        ctl.epoch.store(7, SeqCst);
        let relay = |body: &Value| {
            let _ = body;
            Some((sid.to_owned(), ctl.clone()))
        };
        let mut e = Entry { generation: "gen-1".into(), paused: false, viewers: vec![], join_requests: vec![], link_viewers: 0, at: 0 };
        set(sid, e.clone());
        assert!(admit(&frame(sid, "gen-1", 7, b"ls\r"), relay).is_some());
        e.join_requests = vec![JoinRequest { user_id: "u2".into(), name: "Mira".into(), request_id: "r1".into(), kind: "join".into(), action: None, text: None, label: None }];
        set(sid, e);
        // No paused concept (owner ruling 2026-10-05): while anyone else may type, the band offers
        // no Allow and the owner answers in Lightbulb, so a writer's keys need not be dropped.
        assert!(admit(&frame(sid, "gen-1", 7, b"ls\r"), relay).is_some());
    }

    #[test]
    fn remote_input_writes_the_panel_size_before_its_keys_and_a_size_alone() {
        let (holder, mut peer) = UnixStream::pair().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let ctl = Ctl::new(0);
        let (tx, rx) = sync_channel(QUEUE);
        tx.send((b"ls\r".to_vec(), true, None, Some((120, 40, true)))).unwrap();
        tx.send((vec![], false, None, Some((100, 30, false)))).unwrap();
        tx.send((vec![], true, None, Some((54, 20, true)))).unwrap();
        drop(tx);
        let holder = std::cell::RefCell::new(Some(holder));
        write_with("sid", "gen", &ctl, &rx, |_, _| holder.borrow_mut().take().map(|conn| (conn, true, "h".to_owned())));
        assert_eq!(read_frame(&mut peer).unwrap(), (RESIZE, vec![0, 120, 0, 40, 1]));
        assert_eq!(read_frame(&mut peer).unwrap(), (OWNER_INPUT, b"ls\r".to_vec()));
        assert_eq!(read_frame(&mut peer).unwrap(), (RESIZE, vec![0, 100, 0, 30, 0]), "a size alone, no keys");
        assert_eq!(read_frame(&mut peer).unwrap(), (RESIZE, vec![0, 54, 0, 20, 0]),
            "opening the session in a page claims nothing: the owner's terminal keeps its size until the page types");
    }

    #[test]
    fn remote_input_writer_closes_its_type_client_when_the_relay_stops() {
        let ctl = Ctl::new(0);
        ctl.stop.store(true, SeqCst);
        let (tx, rx) = sync_channel(1);
        tx.send((b"never".to_vec(), false, None, None)).unwrap();
        let started = Instant::now();
        write(SID, "gen-1", &ctl, &rx);
        assert!(started.elapsed() < Duration::from_secs(1));
        assert_eq!(rx.try_recv().unwrap().0, b"never", "nothing is written once the relay stopped");
    }
}
