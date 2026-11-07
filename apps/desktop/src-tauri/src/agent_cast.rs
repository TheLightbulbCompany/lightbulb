//! Live view of the tab a Mac agent run is driving.
//!
//! `browser.cast.set {channel, view, thumb}` from the backend starts, updates or stops one cast
//! per CDP channel (`Channels::cast_set`). The cast opens its OWN connection to the channel's
//! Chrome, attaches to the tab the agent last drove (`Channels::current_target`), and sends
//! `browser.cast.frame` events up the node socket: screencast frames for the view tier, a small
//! screenshot every `THUMB_INTERVAL_MS` for the thumb tier. Latest-wins everywhere: frames are
//! never queued. It stops when both tiers are off, when no set arrives for `CAST_SET_TTL_S`, or
//! when its channel goes away (the channel aborts it).

use crate::agent_chrome::{cdp_command, connect_chrome, Channels};
use crate::node_client::{event_text, EventSender};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::time::MissedTickBehavior;
use tokio_tungstenite::tungstenite::Message;

pub const MAX_FRAME_B64: usize = 200_000;
pub const VIEW_MAX_FPS: u64 = 10;
pub const THUMB_INTERVAL_MS: u64 = 500;
pub const CAST_SET_TTL_S: u64 = 15;
const TICK: Duration = Duration::from_millis(250);
const QUALITY_FLOOR: u8 = 35;
const QUALITY_CEILING: u8 = 80;
/// A frame this small counts toward stepping quality back up.
const SMALL_FRAME: usize = 90_000;
const SMALL_RUN: u32 = 20;
const THUMB_WIDTH: f64 = 480.0;

/// Screencast quality/size, adapted to keep a frame under `MAX_FRAME_B64`.
pub struct Adapt {
    pub quality: u8,
    pub width: u32,
    small_run: u32,
}

impl Default for Adapt {
    fn default() -> Self {
        Self::new()
    }
}

impl Adapt {
    pub fn new() -> Self {
        Self { quality: 70, width: 1280, small_run: 0 }
    }

    /// Some(new screencast params) when the cast must restart; b64_len is the frame's length.
    pub fn observe(&mut self, b64_len: usize) -> Option<Value> {
        if b64_len > MAX_FRAME_B64 {
            self.small_run = 0;
            if self.quality > QUALITY_FLOOR {
                self.quality = (self.quality - 10).max(QUALITY_FLOOR);
            } else {
                self.width = match self.width {
                    1280 => 1024,
                    1024 => 800,
                    _ => return None,
                };
            }
            return Some(screencast_params(self));
        }
        if b64_len >= SMALL_FRAME {
            self.small_run = 0;
            return None;
        }
        self.small_run += 1;
        if self.small_run < SMALL_RUN {
            return None;
        }
        self.small_run = 0;
        if self.quality >= QUALITY_CEILING {
            return None;
        }
        self.quality = (self.quality + 5).min(QUALITY_CEILING);
        Some(screencast_params(self))
    }
}

pub fn screencast_params(a: &Adapt) -> Value {
    json!({"format": "jpeg", "quality": a.quality, "maxWidth": a.width, "maxHeight": a.width})
}

/// `Page.captureScreenshot` params for a 480-CSS-px-wide JPEG of the viewport. The caller sets
/// `clip.x`/`clip.y` to the scroll offset (clip is in page coordinates).
pub fn thumb_params(css_w: f64, css_h: f64) -> Value {
    json!({"format": "jpeg", "quality": 50,
        "clip": {"x": 0.0, "y": 0.0, "width": css_w, "height": css_h, "scale": THUMB_WIDTH / css_w.max(1.0)}})
}

/// The encoded `browser.cast.frame` node event, or None over `MAX_FRAME_B64`.
pub fn frame_event(channel: &str, tier: &str, seq: u64, b64: &str, w: u32, h: u32) -> Option<String> {
    (b64.len() <= MAX_FRAME_B64).then(|| {
        event_text(
            "browser.cast.frame",
            json!({"channel": channel, "tier": tier, "seq": seq, "jpegBase64": b64, "w": w, "h": h}),
        )
    })
}

pub const MAX_INPUT_EVENTS: usize = 32;
pub const MAX_TEXT_CHARS: usize = 1000;
pub const MAX_DELTA_Y: f64 = 5000.0;

/// `cdp_mux.SCROLL_JS`, byte for byte: scroll the nearest scrollable box under (x, y), else the
/// page. A background tab's compositor ignores a synthetic wheel. Only numbers are formatted in.
fn scroll_js(x: f64, y: f64, dy: f64) -> String {
    format!(
        "(() => {{ let e = document.elementFromPoint({x:.6}, {y:.6}); const dy = {dy:.6}; \
         while (e && !(e.scrollHeight > e.clientHeight && /auto|scroll/.test(getComputedStyle(e).overflowY))) \
         e = e.parentElement; (e || document.scrollingElement).scrollBy(0, dy); }})()"
    )
}

/// `cdp_mux.KEYS`: keys a watcher can press (windowsVirtualKeyCode, text).
const KEYS: [(&str, u32, &str); 13] = [
    ("Enter", 13, "\r"), ("Backspace", 8, ""), ("Tab", 9, ""), ("Escape", 27, ""), ("Delete", 46, ""),
    ("ArrowLeft", 37, ""), ("ArrowUp", 38, ""), ("ArrowRight", 39, ""), ("ArrowDown", 40, ""),
    ("Home", 36, ""), ("End", 35, ""), ("PageUp", 33, ""), ("PageDown", 34, ""),
];
/// macOS Chrome runs no editing command for a synthetic key event on its own, so the keyDown
/// names it: Playwright's `macEditingCommands` (trailing `:` dropped) for these keys, plain and
/// with Shift. Tab has no entry there: it moves focus natively.
fn mac_command(key: &str, modifiers: u64) -> Option<&'static str> {
    let shift = match modifiers {
        0 => false,
        SHIFT => true,
        _ => return None,
    };
    Some(match (key, shift) {
        ("Backspace", _) => "deleteBackward",
        ("Delete", _) => "deleteForward",
        ("Enter", _) => "insertNewline",
        ("Escape", _) => "cancelOperation",
        ("ArrowUp", false) => "moveUp",
        ("ArrowDown", false) => "moveDown",
        ("ArrowLeft", false) => "moveLeft",
        ("ArrowRight", false) => "moveRight",
        ("ArrowUp", true) => "moveUpAndModifySelection",
        ("ArrowDown", true) => "moveDownAndModifySelection",
        ("ArrowLeft", true) => "moveLeftAndModifySelection",
        ("ArrowRight", true) => "moveRightAndModifySelection",
        ("Home", false) => "scrollToBeginningOfDocument",
        ("End", false) => "scrollToEndOfDocument",
        ("Home", true) => "moveToBeginningOfDocumentAndModifySelection",
        ("End", true) => "moveToEndOfDocumentAndModifySelection",
        ("PageUp", false) => "scrollPageUp",
        ("PageDown", false) => "scrollPageDown",
        ("PageUp", true) => "pageUpAndModifySelection",
        ("PageDown", true) => "pageDownAndModifySelection",
        _ => return None,
    })
}
const CTRL: u64 = 2;
const META: u64 = 4;
const SHIFT: u64 = 8;

fn button_bit(button: &str) -> u64 {
    match button {
        "left" => 1,
        "right" => 2,
        _ => 4,
    }
}

/// CDP commands for one viewer event, clamped to the frame (w, h). Err on anything unsupported.
pub fn input_commands(event: &Value, w: f64, h: f64) -> Result<Vec<(&'static str, Value)>, ()> {
    let num = |k: &str| event.get(k).and_then(Value::as_f64).ok_or(());
    let point = || -> Result<(f64, f64), ()> { Ok((num("x")?.clamp(0.0, w.max(0.0)), num("y")?.clamp(0.0, h.max(0.0)))) };
    let text = || -> Result<&str, ()> {
        let t = event.get("text").and_then(Value::as_str).ok_or(())?;
        (t.chars().count() <= MAX_TEXT_CHARS).then_some(t).ok_or(())
    };
    let mouse = "Input.dispatchMouseEvent";
    let key_event = "Input.dispatchKeyEvent";
    match event.get("type").and_then(Value::as_str).ok_or(())? {
        kind @ ("down" | "up") => {
            let (x, y) = point()?;
            let button = event.get("button").and_then(Value::as_str).filter(|b| matches!(*b, "left" | "middle" | "right")).ok_or(())?;
            let clicks = event.get("clicks").and_then(Value::as_u64).filter(|c| (1..=3).contains(c)).ok_or(())?;
            let (phase, buttons) = if kind == "down" { ("mousePressed", button_bit(button)) } else { ("mouseReleased", 0) };
            Ok(vec![(mouse, json!({"type": phase, "x": x, "y": y, "button": button, "buttons": buttons, "clickCount": clicks}))])
        }
        "move" => {
            let (x, y) = point()?;
            Ok(vec![(mouse, json!({"type": "mouseMoved", "x": x, "y": y}))])
        }
        "wheel" => {
            let (x, y) = point()?;
            let dy = num("deltaY")?;
            if dy.abs() > MAX_DELTA_Y {
                return Err(());
            }
            Ok(vec![("Runtime.evaluate", json!({"expression": scroll_js(x, y, dy)}))])
        }
        "key" => {
            let key = event.get("key").and_then(Value::as_str).ok_or(())?;
            let modifiers = match event.get("modifiers") {
                None => 0,
                Some(m) => m.as_u64().filter(|m| *m <= 15).ok_or(())?,
            };
            if let Some(&(name, code, text)) = KEYS.iter().find(|(name, _, _)| *name == key) {
                let mut down = json!({"type": if text.is_empty() { "rawKeyDown" } else { "keyDown" },
                    "key": name, "code": name, "windowsVirtualKeyCode": code, "modifiers": modifiers});
                if !text.is_empty() {
                    down["text"] = json!(text);
                }
                if let Some(command) = mac_command(name, modifiers) {
                    down["commands"] = json!([command]);
                }
                let up = json!({"type": "keyUp", "key": name, "code": name, "windowsVirtualKeyCode": code, "modifiers": modifiers});
                return Ok(vec![(key_event, down), (key_event, up)]);
            }
            // macOS Chrome needs the editing command itself; a synthetic Meta+A does nothing.
            if modifiers & (CTRL | META) == 0 {
                return Err(());
            }
            let command = match key.to_ascii_lowercase().as_str() {
                "a" => "selectAll",
                "z" if modifiers & SHIFT != 0 => "redo",
                "z" => "undo",
                "y" => "redo",
                _ => return Err(()),
            };
            let upper = key.to_ascii_uppercase();
            let code = upper.as_bytes()[0] as u32;
            let base = json!({"key": key, "code": format!("Key{upper}"), "windowsVirtualKeyCode": code, "modifiers": modifiers});
            let mut down = base.clone();
            down["type"] = json!("keyDown");
            down["commands"] = json!([command]);
            let mut up = base;
            up["type"] = json!("keyUp");
            Ok(vec![(key_event, down), (key_event, up)])
        }
        "text" => Ok(text()?
            .chars()
            .flat_map(|c| {
                let c = c.to_string();
                [
                    (key_event, json!({"type": "keyDown", "key": c, "text": c})),
                    (key_event, json!({"type": "keyUp", "key": c})),
                ]
            })
            .collect()),
        "paste" => Ok(vec![("Input.insertText", json!({"text": text()?}))]),
        _ => Err(()),
    }
}

/// Every event's commands, in order, or Err if the batch is not a list of at most
/// `MAX_INPUT_EVENTS` supported events: a bad event never half-applies.
pub fn batch_commands(events: &Value, w: f64, h: f64) -> Result<Vec<(&'static str, Value)>, ()> {
    let events = events.as_array().filter(|e| e.len() <= MAX_INPUT_EVENTS).ok_or(())?;
    let mut out = Vec::new();
    for event in events {
        out.extend(input_commands(event, w, h)?);
    }
    Ok(out)
}

/// Track the button a `mousePressed` holds, and put it on each `mouseMoved` until the release,
/// so drags and selections work.
fn hold_buttons(held: &mut Option<String>, params: &mut Value) {
    match params.get("type").and_then(Value::as_str) {
        Some("mousePressed") => *held = params.get("button").and_then(Value::as_str).map(String::from),
        Some("mouseReleased") => *held = None,
        Some("mouseMoved") => {
            if let Some(b) = held.as_deref() {
                params["button"] = json!(b);
                params["buttons"] = json!(button_bit(b));
            }
        }
        _ => {}
    }
}

/// The slot's value when the node lane is idle, else None with the slot kept, so the newest
/// frame goes as soon as the lane drains and nothing stale queues behind the agent's traffic.
fn take_if_idle<T>(out: &EventSender, slot: &mut Option<T>) -> Option<T> {
    if out.is_idle() {
        slot.take()
    } else {
        None
    }
}

/// Acknowledge a screencast frame (`params` is `{sessionId}` from the frame) on `session`.
fn frame_ack(id: u64, params: Value, session: &str) -> Message {
    cdp_command(id, "Page.screencastFrameAck", params, Some(session))
}

fn epoch_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0)
}

fn log(msg: &str) {
    crate::log(&format!("[agent-cast] {msg}"));
}

#[derive(Clone, Copy)]
pub struct Tiers {
    pub view: bool,
    pub thumb: bool,
    pub set_at: Instant,
}

/// One running cast. `Channels` owns it; dropping the handle does not stop the task, `abort` does.
pub struct CastHandle {
    tiers: Arc<Mutex<Tiers>>,
    input: tokio::sync::mpsc::Sender<Value>,
    pub task: tokio::task::JoinHandle<()>,
}

impl CastHandle {
    pub fn spawn(channels: Channels, out: EventSender, channel: String, view: bool, thumb: bool) -> Self {
        let tiers = Arc::new(Mutex::new(Tiers { view, thumb, set_at: Instant::now() }));
        let (input, input_rx) = tokio::sync::mpsc::channel(8);
        let task = tokio::spawn(run(channels, out, channel, tiers.clone(), input_rx));
        Self { tiers, input, task }
    }

    /// A viewer's `browser.cast.input` events. Dropped when the cast is behind (latest input
    /// is not worth queueing past a handful of batches).
    pub fn input(&self, events: Value) {
        let _ = self.input.try_send(events);
    }

    /// A keepalive or tier change from `browser.cast.set`.
    pub fn set(&self, view: bool, thumb: bool) {
        *self.tiers.lock().unwrap_or_else(|e| e.into_inner()) = Tiers { view, thumb, set_at: Instant::now() };
    }
}

async fn run(channels: Channels, out: EventSender, channel: String, tiers: Arc<Mutex<Tiers>>, mut input: tokio::sync::mpsc::Receiver<Value>) {
    log(&format!("channel {channel}: cast started"));
    let why = cast(&channels, &out, &channel, &tiers, &mut input).await;
    log(&format!("channel {channel}: cast stopped ({why})"));
}

/// Why the cast should stop now, if it should.
fn stop_reason(tiers: &Mutex<Tiers>) -> Option<&'static str> {
    let t = *tiers.lock().unwrap_or_else(|e| e.into_inner());
    if !t.view && !t.thumb {
        Some("off")
    } else if t.set_at.elapsed() > Duration::from_secs(CAST_SET_TTL_S) {
        Some("ttl")
    } else {
        None
    }
}

async fn cast(
    channels: &Channels,
    out: &EventSender,
    channel: &str,
    tiers: &Mutex<Tiers>,
    input: &mut tokio::sync::mpsc::Receiver<Value>,
) -> &'static str {
    let mut tick = tokio::time::interval(TICK);
    tick.set_missed_tick_behavior(MissedTickBehavior::Delay);
    // The channel may still be opening its Chrome.
    let url = loop {
        tick.tick().await;
        if let Some(why) = stop_reason(tiers) {
            return why;
        }
        if let Some(url) = channels.endpoint_url(channel) {
            break url;
        }
    };
    let Ok(ws) = connect_chrome(&url).await else { return "connect failed" };
    let (mut to_chrome, mut from_chrome) = ws.split();
    let mut send_tick = tokio::time::interval(Duration::from_millis(1000 / VIEW_MAX_FPS));
    send_tick.set_missed_tick_behavior(MissedTickBehavior::Delay);

    let mut next = 0u64;
    let mut id = || {
        next += 1;
        next
    };
    let mut adapt = Adapt::new();
    // The target we attached (or are attaching) to, and our session on it.
    let mut attached: Option<String> = None;
    let mut session: Option<String> = None;
    let mut attach_id: Option<u64> = None;
    let mut screencasting = false;
    let mut latest: Option<(String, u32, u32)> = None;
    // The kept frame's Page.screencastFrameAck, sent once the frame is handed to the node lane, so
    // Chrome paces its encoding to the uplink.
    let mut pending_ack: Option<Value> = None;
    // Thumb: in-flight `Page.getLayoutMetrics`, then `Page.captureScreenshot` (with its size).
    let mut layout_id: Option<u64> = None;
    let mut shot: Option<(u64, u32, u32)> = None;
    let mut last_thumb_at: Option<Instant> = None;
    let mut last_thumb = String::new();
    // The latest frame's CSS size (viewer input is clamped to it) and the viewer's held button.
    let mut frame_size: Option<(f64, f64)> = None;
    let mut held: Option<String> = None;

    let why = loop {
        tokio::select! {
            _ = tick.tick() => {
                if let Some(why) = stop_reason(tiers) {
                    break why;
                }
                let t = *tiers.lock().unwrap_or_else(|e| e.into_inner());
                let want = channels.current_target(channel);
                if want != attached {
                    if let Some(s) = session.take() {
                        let cmd = cdp_command(id(), "Target.detachFromTarget", json!({"sessionId": s}), None);
                        if to_chrome.send(cmd).await.is_err() { break "chrome closed" }
                    }
                    (screencasting, latest, layout_id, shot, attach_id) = (false, None, None, None, None);
                    (frame_size, held, pending_ack) = (None, None, None);
                    attached = want;
                    if let Some(target) = &attached {
                        let n = id();
                        attach_id = Some(n);
                        let cmd = cdp_command(n, "Target.attachToTarget", json!({"targetId": target, "flatten": true}), None);
                        if to_chrome.send(cmd).await.is_err() { break "chrome closed" }
                    }
                }
                let Some(s) = session.as_deref() else { continue };
                if t.view != screencasting {
                    let cmd = if t.view {
                        cdp_command(id(), "Page.startScreencast", screencast_params(&adapt), Some(s))
                    } else {
                        (latest, pending_ack) = (None, None);
                        cdp_command(id(), "Page.stopScreencast", json!({}), Some(s))
                    };
                    if to_chrome.send(cmd).await.is_err() { break "chrome closed" }
                    screencasting = t.view;
                }
                let due = last_thumb_at.is_none_or(|at| at.elapsed() >= Duration::from_millis(THUMB_INTERVAL_MS));
                if t.thumb && due && layout_id.is_none() && shot.is_none() {
                    let n = id();
                    layout_id = Some(n);
                    last_thumb_at = Some(Instant::now());
                    if to_chrome.send(cdp_command(n, "Page.getLayoutMetrics", json!({}), Some(s))).await.is_err() {
                        break "chrome closed";
                    }
                }
            }
            Some(events) = input.recv() => {
                // No session or no frame yet: the viewer is looking at nothing, so drop it.
                let (Some(s), Some((w, h))) = (session.clone(), frame_size) else { continue };
                let Ok(commands) = batch_commands(&events, w, h) else {
                    log(&format!("channel {channel}: viewer input rejected"));
                    continue;
                };
                // CDP runs one connection's messages in order, so no reply is awaited in between.
                channels.note_viewer_input();
                let mut sent = true;
                for (method, mut params) in commands {
                    hold_buttons(&mut held, &mut params);
                    sent = sent && to_chrome.send(cdp_command(id(), method, params, Some(&s))).await.is_ok();
                }
                if !sent { break "chrome closed" }
                channels.human_input_now(channel);
            }
            _ = send_tick.tick() => {
                if let Some((data, w, h)) = take_if_idle(out, &mut latest) {
                    if let Some(text) = frame_event(channel, "view", epoch_ms(), &data, w, h) {
                        if out.send_text(text).await.is_err() { break "node socket closed" }
                    }
                    if let (Some(ack), Some(s)) = (pending_ack.take(), session.as_deref()) {
                        if to_chrome.send(frame_ack(id(), ack, s)).await.is_err() {
                            break "chrome closed";
                        }
                    }
                }
            }
            msg = from_chrome.next() => {
                let text = match msg {
                    Some(Ok(Message::Text(text))) => text,
                    Some(Ok(_)) => continue,
                    _ => break "chrome closed",
                };
                let Ok(v) = serde_json::from_str::<Value>(text.as_str()) else { continue };
                if let Some(n) = v.get("id").and_then(Value::as_u64) {
                    if Some(n) == attach_id {
                        attach_id = None;
                        session = v.pointer("/result/sessionId").and_then(Value::as_str).map(String::from);
                        if session.is_none() {
                            // The attach failed: forget the target so the next tick retries.
                            attached = None;
                        }
                        if let Some(s) = session.as_deref() {
                            let cmd = cdp_command(id(), "Emulation.setFocusEmulationEnabled", json!({"enabled": true}), Some(s));
                            if to_chrome.send(cmd).await.is_err() { break "chrome closed" }
                        }
                    } else if Some(n) == layout_id {
                        layout_id = None;
                        let (Some(s), Some(vp)) = (session.as_deref(), v.pointer("/result/cssVisualViewport")) else { continue };
                        let num = |k: &str| vp.get(k).and_then(Value::as_f64).unwrap_or(0.0);
                        let (w, h) = (num("clientWidth"), num("clientHeight"));
                        if w < 1.0 || h < 1.0 { continue }
                        let mut params = thumb_params(w, h);
                        params["clip"]["x"] = json!(num("pageX"));
                        params["clip"]["y"] = json!(num("pageY"));
                        let n = id();
                        shot = Some((n, w.round() as u32, h.round() as u32));
                        if to_chrome.send(cdp_command(n, "Page.captureScreenshot", params, Some(s))).await.is_err() {
                            break "chrome closed";
                        }
                    } else if let Some((_, w, h)) = shot.filter(|(sid, _, _)| *sid == n) {
                        shot = None;
                        let Some(data) = v.pointer("/result/data").and_then(Value::as_str) else { continue };
                        frame_size = Some((w as f64, h as f64));
                        // A busy lane drops the thumb; the next one comes in THUMB_INTERVAL_MS.
                        if data == last_thumb || !out.is_idle() { continue }
                        if let Some(text) = frame_event(channel, "thumb", epoch_ms(), data, w, h) {
                            if out.send_text(text).await.is_err() { break "node socket closed" }
                        }
                        last_thumb = data.to_string();
                    }
                    continue;
                }
                let from_ours = session.is_some() && v.get("sessionId").and_then(Value::as_str) == session.as_deref();
                match v.get("method").and_then(Value::as_str) {
                    Some("Page.screencastFrame") if from_ours => {
                        let s = session.clone().unwrap_or_default();
                        let ack = json!({"sessionId": v.pointer("/params/sessionId").cloned().unwrap_or(json!(0))});
                        let data = v.pointer("/params/data").and_then(Value::as_str).filter(|_| screencasting);
                        // A frame kept for sending is acked when it is handed to the node lane.
                        // One that lands after Page.stopScreencast is acked now and never kept or
                        // adapted: an adapt restart here would turn the screencast back on while
                        // view is off, and the tick would never stop it.
                        let Some(data) = data else {
                            if to_chrome.send(frame_ack(id(), ack, &s)).await.is_err() {
                                break "chrome closed";
                            }
                            continue;
                        };
                        // A still-unsent frame is superseded; release its ack too.
                        let mut acks = vec![ack];
                        acks.extend(pending_ack.take());
                        if let Some(params) = adapt.observe(data.len()) {
                            // Restart at the new size; this frame is dropped.
                            latest = None;
                            let mut sent = true;
                            for ack in acks {
                                sent = sent && to_chrome.send(frame_ack(id(), ack, &s)).await.is_ok();
                            }
                            for (method, p) in [("Page.stopScreencast", json!({})), ("Page.startScreencast", params)] {
                                sent = sent && to_chrome.send(cdp_command(id(), method, p, Some(&s))).await.is_ok();
                            }
                            if !sent { break "chrome closed" }
                            continue;
                        }
                        let dim = |k: &str| v.pointer(&format!("/params/metadata/{k}")).and_then(Value::as_f64).unwrap_or(0.0);
                        let (w, h) = (dim("deviceWidth"), dim("deviceHeight"));
                        // A frame without its size must not pin every input to the origin.
                        if w >= 1.0 && h >= 1.0 {
                            frame_size = Some((w, h));
                        }
                        let mut sent = true;
                        for old in acks.drain(1..) {
                            sent = sent && to_chrome.send(frame_ack(id(), old, &s)).await.is_ok();
                        }
                        if !sent { break "chrome closed" }
                        pending_ack = acks.pop();
                        latest = Some((data.to_string(), w.round() as u32, h.round() as u32));
                    }
                    Some("Target.detachedFromTarget")
                        if v.pointer("/params/sessionId").and_then(Value::as_str) == session.as_deref() && session.is_some() =>
                    {
                        // Our session is gone (the tab closed or crashed): re-attach on a later tick.
                        (session, attached, screencasting, latest, layout_id, shot) = (None, None, false, None, None, None);
                        (frame_size, held, pending_ack) = (None, None, None);
                    }
                    _ => {}
                }
            }
        }
    };
    if let Some(s) = session {
        let _ = to_chrome.send(cdp_command(id(), "Target.detachFromTarget", json!({"sessionId": s}), None)).await;
    }
    let _ = to_chrome.close().await;
    why
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const BIG: usize = MAX_FRAME_B64 + 1;

    #[test]
    fn oversize_frames_step_quality_to_the_floor_then_width_down() {
        let mut a = Adapt::new();
        assert_eq!((a.quality, a.width), (70, 1280));
        for q in [60, 50, 40, 35] {
            let p = a.observe(BIG).expect("quality changed");
            assert_eq!(p, json!({"format": "jpeg", "quality": q, "maxWidth": 1280, "maxHeight": 1280}));
        }
        assert_eq!(a.observe(BIG).unwrap()["maxWidth"], 1024);
        assert_eq!(a.observe(BIG).unwrap(), json!({"format": "jpeg", "quality": 35, "maxWidth": 800, "maxHeight": 800}));
        assert_eq!(a.observe(BIG), None, "floor and narrowest width hold");
        assert_eq!((a.quality, a.width), (35, 800));
    }

    #[test]
    fn twenty_small_frames_step_quality_up_to_the_ceiling() {
        let mut a = Adapt::new();
        for _ in 0..19 {
            assert_eq!(a.observe(1000), None);
        }
        assert_eq!(a.observe(1000).unwrap()["quality"], 75);
        for _ in 0..19 {
            assert_eq!(a.observe(1000), None);
        }
        assert_eq!(a.observe(1000).unwrap()["quality"], 80);
        for _ in 0..40 {
            assert_eq!(a.observe(1000), None, "ceiling holds");
        }
        assert_eq!(a.quality, 80);
    }

    #[test]
    fn a_mid_size_or_oversize_frame_resets_the_small_run() {
        let mut a = Adapt::new();
        for _ in 0..19 {
            a.observe(1000);
        }
        assert_eq!(a.observe(150_000), None);
        for _ in 0..19 {
            assert_eq!(a.observe(1000), None);
        }
        assert_eq!(a.observe(1000).unwrap()["quality"], 75);
    }

    #[test]
    fn thumb_params_scale_to_480_css_px_wide() {
        let p = thumb_params(1440.0, 900.0);
        assert_eq!(p["format"], "jpeg");
        assert_eq!(p["quality"], 50);
        assert_eq!(p["clip"]["width"], 1440.0);
        assert_eq!(p["clip"]["height"], 900.0);
        assert_eq!(p["clip"]["scale"].as_f64().unwrap(), 480.0 / 1440.0);
        assert_eq!(thumb_params(0.0, 0.0)["clip"]["scale"], 480.0, "never divides by zero");
    }

    #[test]
    fn frame_event_shape_and_cap() {
        let text = frame_event("c1", "view", 1_700_000_000_123, "abc", 1280, 800).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&text).unwrap(),
            json!({"type": "event", "event": "browser.cast.frame", "payload": {
                "channel": "c1", "tier": "view", "seq": 1_700_000_000_123u64, "jpegBase64": "abc", "w": 1280, "h": 800}})
        );
        assert!(frame_event("c1", "thumb", 1, &"a".repeat(MAX_FRAME_B64), 1, 1).is_some());
        assert_eq!(frame_event("c1", "thumb", 1, &"a".repeat(MAX_FRAME_B64 + 1), 1, 1), None);
    }

    // ---- viewer input (Task 4) -----------------------------------------------------------

    fn cmds(event: Value) -> Vec<(&'static str, Value)> {
        input_commands(&event, 800.0, 600.0).expect("supported")
    }

    #[test]
    fn mouse_down_up_move_map_to_dispatch_mouse_event_clamped_to_the_frame() {
        assert_eq!(
            cmds(json!({"type": "down", "x": 900.0, "y": -5.0, "button": "right", "clicks": 2})),
            [("Input.dispatchMouseEvent", json!({"type": "mousePressed", "x": 800.0, "y": 0.0, "button": "right", "buttons": 2, "clickCount": 2}))]
        );
        assert_eq!(
            cmds(json!({"type": "up", "x": 10, "y": 700, "button": "left", "clicks": 1})),
            [("Input.dispatchMouseEvent", json!({"type": "mouseReleased", "x": 10.0, "y": 600.0, "button": "left", "buttons": 0, "clickCount": 1}))]
        );
        assert_eq!(
            cmds(json!({"type": "move", "x": 5, "y": 6})),
            [("Input.dispatchMouseEvent", json!({"type": "mouseMoved", "x": 5.0, "y": 6.0}))]
        );
        for bad in [
            json!({"type": "down", "x": 1, "y": 1, "button": "back", "clicks": 1}),
            json!({"type": "down", "x": 1, "y": 1, "button": "left", "clicks": 4}),
            json!({"type": "down", "x": 1, "y": 1, "button": "left", "clicks": 0}),
            json!({"type": "down", "x": "1", "y": 1, "button": "left", "clicks": 1}),
            json!({"type": "move", "y": 1}),
        ] {
            assert_eq!(input_commands(&bad, 800.0, 600.0), Err(()), "{bad}");
        }
    }

    #[test]
    fn wheel_runs_the_ported_scroll_js_with_clamped_numbers() {
        let got = cmds(json!({"type": "wheel", "x": 1000, "y": 20.5, "deltaY": -120}));
        assert_eq!(got.len(), 1);
        assert_eq!(got[0].0, "Runtime.evaluate");
        assert_eq!(
            got[0].1["expression"],
            "(() => { let e = document.elementFromPoint(800.000000, 20.500000); const dy = -120.000000; \
             while (e && !(e.scrollHeight > e.clientHeight && /auto|scroll/.test(getComputedStyle(e).overflowY))) \
             e = e.parentElement; (e || document.scrollingElement).scrollBy(0, dy); })()"
        );
        assert!(input_commands(&json!({"type": "wheel", "x": 1, "y": 1, "deltaY": 5000}), 800.0, 600.0).is_ok());
        assert_eq!(input_commands(&json!({"type": "wheel", "x": 1, "y": 1, "deltaY": -5001}), 800.0, 600.0), Err(()));
    }

    #[test]
    fn keys_come_from_the_ported_table_with_modifiers() {
        assert_eq!(
            cmds(json!({"type": "key", "key": "Enter", "modifiers": 0})),
            [
                ("Input.dispatchKeyEvent", json!({"type": "keyDown", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 0, "text": "\r",
                    "commands": ["insertNewline"]})),
                ("Input.dispatchKeyEvent", json!({"type": "keyUp", "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 0})),
            ]
        );
        let shift_left = cmds(json!({"type": "key", "key": "ArrowLeft", "modifiers": 8}));
        assert_eq!(
            shift_left[0].1,
            json!({"type": "rawKeyDown", "key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37, "modifiers": 8,
                "commands": ["moveLeftAndModifySelection"]})
        );
        for bad in [
            json!({"type": "key", "key": "F13", "modifiers": 0}),
            json!({"type": "key", "key": "a", "modifiers": 0}),
            json!({"type": "key", "key": "Enter", "modifiers": 16}),
            json!({"type": "key", "key": "a", "modifiers": 1}),
        ] {
            assert_eq!(input_commands(&bad, 800.0, 600.0), Err(()), "{bad}");
        }
    }

    #[test]
    fn named_keys_carry_the_mac_editing_command() {
        let command = |key: &str, modifiers: u64| cmds(json!({"type": "key", "key": key, "modifiers": modifiers}))[0].1.get("commands").cloned();
        assert_eq!(command("Backspace", 0), Some(json!(["deleteBackward"])));
        assert_eq!(command("Delete", 8), Some(json!(["deleteForward"])));
        assert_eq!(command("ArrowRight", 0), Some(json!(["moveRight"])));
        assert_eq!(command("ArrowDown", 8), Some(json!(["moveDownAndModifySelection"])));
        assert_eq!(command("Home", 0), Some(json!(["scrollToBeginningOfDocument"])));
        assert_eq!(command("End", 8), Some(json!(["moveToEndOfDocumentAndModifySelection"])));
        assert_eq!(command("PageUp", 0), Some(json!(["scrollPageUp"])));
        assert_eq!(command("PageDown", 8), Some(json!(["pageDownAndModifySelection"])));
        assert_eq!(command("Escape", 0), Some(json!(["cancelOperation"])));
        assert_eq!(command("Tab", 0), None, "Tab moves focus natively; the mac table has no command for it");
        assert_eq!(command("ArrowLeft", 4), None, "only plain and Shift are mapped");
        // The keyUp never carries one.
        assert_eq!(cmds(json!({"type": "key", "key": "Backspace", "modifiers": 0}))[1].1.get("commands"), None);
    }

    #[test]
    fn meta_or_ctrl_shortcuts_map_to_editing_commands() {
        let command = |key: &str, modifiers: u64| {
            let got = cmds(json!({"type": "key", "key": key, "modifiers": modifiers}));
            assert_eq!(got[0].1["type"], "keyDown");
            assert_eq!(got[0].1["modifiers"], modifiers);
            got[0].1["commands"].clone()
        };
        assert_eq!(command("a", 4), json!(["selectAll"]));
        assert_eq!(command("a", 2), json!(["selectAll"]));
        assert_eq!(command("z", 4), json!(["undo"]));
        assert_eq!(command("Z", 4 | 8), json!(["redo"]));
        assert_eq!(command("y", 2), json!(["redo"]));
    }

    #[test]
    fn text_is_two_key_events_per_char_and_paste_is_insert_text() {
        assert_eq!(
            cmds(json!({"type": "text", "text": "hé"})),
            [
                ("Input.dispatchKeyEvent", json!({"type": "keyDown", "key": "h", "text": "h"})),
                ("Input.dispatchKeyEvent", json!({"type": "keyUp", "key": "h"})),
                ("Input.dispatchKeyEvent", json!({"type": "keyDown", "key": "é", "text": "é"})),
                ("Input.dispatchKeyEvent", json!({"type": "keyUp", "key": "é"})),
            ]
        );
        assert_eq!(cmds(json!({"type": "paste", "text": "hello"})), [("Input.insertText", json!({"text": "hello"}))]);
        let max = "é".repeat(MAX_TEXT_CHARS);
        assert_eq!(cmds(json!({"type": "text", "text": max})).len(), 2 * MAX_TEXT_CHARS);
        let over = "a".repeat(MAX_TEXT_CHARS + 1);
        assert_eq!(input_commands(&json!({"type": "text", "text": over}), 1.0, 1.0), Err(()));
        assert_eq!(input_commands(&json!({"type": "paste", "text": over}), 1.0, 1.0), Err(()));
        assert_eq!(input_commands(&json!({"type": "click", "x": 1, "y": 1}), 1.0, 1.0), Err(()), "unknown type");
    }

    #[test]
    fn a_batch_is_all_or_nothing_and_capped() {
        let ok = json!({"type": "move", "x": 1, "y": 1});
        let bad = json!({"type": "key", "key": "F13", "modifiers": 0});
        assert_eq!(batch_commands(&json!([ok, ok]), 10.0, 10.0).unwrap().len(), 2);
        assert_eq!(batch_commands(&json!([ok, bad]), 10.0, 10.0), Err(()), "a bad event rejects the whole batch");
        assert_eq!(batch_commands(&Value::Array(vec![ok.clone(); MAX_INPUT_EVENTS]), 10.0, 10.0).unwrap().len(), MAX_INPUT_EVENTS);
        assert_eq!(batch_commands(&Value::Array(vec![ok; MAX_INPUT_EVENTS + 1]), 10.0, 10.0), Err(()));
        assert_eq!(batch_commands(&json!({}), 10.0, 10.0), Err(()));
    }

    #[test]
    fn frame_ack_is_page_screencast_frame_ack() {
        let Message::Text(text) = frame_ack(3, json!({"sessionId": 7}), "S") else { panic!("text") };
        assert_eq!(
            serde_json::from_str::<Value>(text.as_str()).unwrap(),
            json!({"id": 3, "method": "Page.screencastFrameAck", "params": {"sessionId": 7}, "sessionId": "S"})
        );
    }

    #[tokio::test]
    async fn a_busy_lane_keeps_the_frame_for_later() {
        let (tx, mut rx) = tokio::sync::mpsc::channel(4);
        let out = EventSender::new(tx);
        let mut slot = Some(1);
        assert_eq!(take_if_idle(&out, &mut slot), Some(1), "an idle lane takes the frame");
        let mut slot = Some(2);
        out.send_text("queued".into()).await.unwrap();
        assert_eq!(take_if_idle(&out, &mut slot), None, "a busy lane does not");
        assert_eq!(slot, Some(2), "the frame stays, so the newest goes next");
        rx.recv().await;
        assert_eq!(take_if_idle(&out, &mut slot), Some(2));
    }

    #[test]
    fn a_move_carries_the_held_button() {
        let mut held = None;
        let mut run = |event: Value| {
            let mut c = cmds(event);
            hold_buttons(&mut held, &mut c[0].1);
            c.remove(0).1
        };
        assert_eq!(run(json!({"type": "move", "x": 1, "y": 1})).get("buttons"), None);
        run(json!({"type": "down", "x": 1, "y": 1, "button": "middle", "clicks": 1}));
        let m = run(json!({"type": "move", "x": 2, "y": 2}));
        assert_eq!((&m["button"], &m["buttons"]), (&json!("middle"), &json!(4)));
        run(json!({"type": "up", "x": 2, "y": 2, "button": "middle", "clicks": 1}));
        assert_eq!(run(json!({"type": "move", "x": 3, "y": 3})).get("buttons"), None);
    }
}
