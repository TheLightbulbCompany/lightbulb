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
    pub task: tokio::task::JoinHandle<()>,
}

impl CastHandle {
    pub fn spawn(channels: Channels, out: EventSender, channel: String, view: bool, thumb: bool) -> Self {
        let tiers = Arc::new(Mutex::new(Tiers { view, thumb, set_at: Instant::now() }));
        let task = tokio::spawn(run(channels, out, channel, tiers.clone()));
        Self { tiers, task }
    }

    /// A keepalive or tier change from `browser.cast.set`.
    pub fn set(&self, view: bool, thumb: bool) {
        *self.tiers.lock().unwrap_or_else(|e| e.into_inner()) = Tiers { view, thumb, set_at: Instant::now() };
    }
}

async fn run(channels: Channels, out: EventSender, channel: String, tiers: Arc<Mutex<Tiers>>) {
    log(&format!("channel {channel}: cast started"));
    let why = cast(&channels, &out, &channel, &tiers).await;
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

async fn cast(channels: &Channels, out: &EventSender, channel: &str, tiers: &Mutex<Tiers>) -> &'static str {
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
    // Thumb: in-flight `Page.getLayoutMetrics`, then `Page.captureScreenshot` (with its size).
    let mut layout_id: Option<u64> = None;
    let mut shot: Option<(u64, u32, u32)> = None;
    let mut last_thumb_at: Option<Instant> = None;
    let mut last_thumb = String::new();

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
                        latest = None;
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
            _ = send_tick.tick() => {
                if let Some((data, w, h)) = latest.take() {
                    if let Some(text) = frame_event(channel, "view", epoch_ms(), &data, w, h) {
                        if out.send_text(text).await.is_err() { break "node socket closed" }
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
                        if data == last_thumb { continue }
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
                        if to_chrome.send(cdp_command(id(), "Page.screencastAck", ack, Some(&s))).await.is_err() {
                            break "chrome closed";
                        }
                        let Some(data) = v.pointer("/params/data").and_then(Value::as_str) else { continue };
                        if let Some(params) = adapt.observe(data.len()) {
                            // Restart at the new size; this frame is dropped.
                            latest = None;
                            for (method, p) in [("Page.stopScreencast", json!({})), ("Page.startScreencast", params)] {
                                if to_chrome.send(cdp_command(id(), method, p, Some(&s))).await.is_err() {
                                    return "chrome closed";
                                }
                            }
                            continue;
                        }
                        let dim = |k: &str| v.pointer(&format!("/params/metadata/{k}")).and_then(Value::as_f64).unwrap_or(0.0).round() as u32;
                        latest = Some((data.to_string(), dim("deviceWidth"), dim("deviceHeight")));
                    }
                    Some("Target.detachedFromTarget")
                        if v.pointer("/params/sessionId").and_then(Value::as_str) == session.as_deref() && session.is_some() =>
                    {
                        // Our session is gone (the tab closed or crashed): re-attach on a later tick.
                        (session, attached, screencasting, latest, layout_id, shot) = (None, None, false, None, None, None);
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
}
