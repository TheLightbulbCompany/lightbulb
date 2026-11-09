//! The talk key (founder, 8 Nov 2026): tap 🌐 Fn to start or end a talk with Lightbulb, hold it for
//! push-to-talk. The web's TalkOrb owns the tap/hold rule (HOLD_MS); this sends it the raw key as
//! `lightbulb:talk-key` events: "down", "up", or "cancel" when Fn turned out to be part of a chord
//! (Fn+←, Fn+F5): that was never a talk.
//!
//! Fn is a modifier, and tauri-plugin-global-shortcut cannot register a modifier alone, so an NSEvent
//! monitor watches `flagsChanged` for the Fn key (kVK_Function). A local monitor (this app in front)
//! needs no permission. A global monitor (any app in front) sees key events only once the app is
//! trusted for Accessibility (NSEvent addGlobalMonitorForEvents docs: "Key-related events may only be
//! monitored if accessibility is enabled or if your application is trusted for accessibility access";
//! Input Monitoring is for CGEventTap/IOHID, which this does not use). Until it is, ⌃⌥Space through
//! the global-shortcut plugin does the same job.
//!
//! While the mic is live and the main window is not in front, a small always-on-top "Mic on" pill
//! shows (the web reports the mic with `voice_mic_live`).
//!
//! ponytail: the key is not rebindable; a Settings picker when someone asks for another key.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use tauri::{AppHandle, Manager};

pub const FALLBACK: &str = "ctrl+alt+space";
const KVK_FUNCTION: u16 = 63;
const PILL: &str = "mic-pill";
const ASKED_MARKER: &str = "voice-key-asked";
static MIC_LIVE: AtomicBool = AtomicBool::new(false);
static GLOBAL_ON: AtomicBool = AtomicBool::new(false);
static FN: Mutex<FnKey> = Mutex::new(FnKey { down: false, chord: false });

/// Fn's state from the monitor's events: which talk-key event, if any, each one means.
pub struct FnKey {
    down: bool,
    chord: bool,
}

impl FnKey {
    /// A `flagsChanged` event: `fn_flag` is NSEventModifierFlagFunction in its modifier flags.
    pub fn flags(&mut self, key_code: u16, fn_flag: bool) -> Option<&'static str> {
        if key_code != KVK_FUNCTION || self.down == fn_flag {
            return None;
        }
        self.down = fn_flag;
        if fn_flag {
            self.chord = false;
            Some("down")
        } else if self.chord {
            None // already cancelled
        } else {
            Some("up")
        }
    }

    /// A `keyDown` event: another key while Fn is held makes it a chord, not a talk.
    pub fn key(&mut self) -> Option<&'static str> {
        if !self.down || self.chord {
            return None;
        }
        self.chord = true;
        Some("cancel")
    }
}

/// One talk-key event to the web's TalkOrb (TALK_KEY).
pub fn send(app: &AppHandle, detail: &str) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval(&format!(
            "window.dispatchEvent(new CustomEvent('lightbulb:talk-key',{{detail:'{detail}'}}))"
        ));
    }
}

/// At launch: the local Fn monitor, and the global one if trusted, else ⌃⌥Space until it is.
pub fn install(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    {
        let a = app.clone();
        let _ = app.run_on_main_thread(move || mac::monitor(a, false));
        if mac::trusted() {
            global_on(app);
        } else {
            fallback(app, true);
            let a = app.clone();
            // The person may grant Accessibility at any time; then Fn takes over from ⌃⌥Space.
            std::thread::spawn(move || {
                while !mac::trusted() {
                    std::thread::sleep(std::time::Duration::from_secs(2));
                }
                fallback(&a, false);
                global_on(&a);
            });
        }
    }
}

#[cfg(target_os = "macos")]
fn global_on(app: &AppHandle) {
    if GLOBAL_ON.swap(true, Ordering::SeqCst) {
        return;
    }
    let a = app.clone();
    let _ = app.run_on_main_thread(move || mac::monitor(a, true));
    crate::log("[voice-key] Fn is the talk key");
}

fn fallback(app: &AppHandle, on: bool) {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};
    let gs = app.global_shortcut();
    let res = if on {
        gs.on_shortcut(FALLBACK, |app, _shortcut, event| {
            send(app, if event.state == ShortcutState::Pressed { "down" } else { "up" });
        })
    } else {
        gs.unregister(FALLBACK)
    };
    if let Err(e) = res {
        crate::log(&format!("[voice-key] {FALLBACK} {}: {e}", if on { "register" } else { "unregister" }));
    }
}

/// The web: the mic is live (a talk is on and not muted), or not. The first live mic on this Mac
/// also asks, once, for what Fn needs.
#[tauri::command]
pub async fn voice_mic_live(app: AppHandle, live: bool) {
    MIC_LIVE.store(live, Ordering::SeqCst);
    refresh_pill(&app);
    #[cfg(target_os = "macos")]
    if live {
        ask_once(&app).await;
    }
}

/// Show the "Mic on" pill while the mic is live and the main window is not in front.
pub fn refresh_pill(app: &AppHandle) {
    let front = app.get_webview_window("main").and_then(|w| w.is_focused().ok()).unwrap_or(false);
    let show = MIC_LIVE.load(Ordering::SeqCst) && !front;
    match (app.get_webview_window(PILL), show) {
        (Some(p), true) => {
            let _ = p.show();
        }
        (Some(p), false) => {
            let _ = p.hide();
        }
        (None, true) => {
            if let Err(e) = pill(app) {
                crate::log(&format!("[voice-key] mic pill: {e}"));
            }
        }
        (None, false) => {}
    }
}

const PILL_HTML: &str = "<!doctype html><meta charset=utf-8><style>html,body{margin:0;height:100%;background:#1A1A1C;\
color:#fff;font:600 12px -apple-system,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;\
gap:7px;cursor:default;-webkit-user-select:none}i{width:8px;height:8px;border-radius:50%;background:#4FAE6A}</style>\
<i></i>Mic on";

fn pill(app: &AppHandle) -> tauri::Result<()> {
    use tauri::{WebviewUrl, WebviewWindowBuilder};
    let url = format!("data:text/html,{}", urlencoding::encode(PILL_HTML));
    let w = WebviewWindowBuilder::new(app, PILL, WebviewUrl::External(url.parse().expect("data url")))
        .title("Lightbulb mic")
        .inner_size(92.0, 28.0)
        .resizable(false)
        .decorations(false)
        .always_on_top(true)
        .visible_on_all_workspaces(true)
        .skip_taskbar(true)
        .focused(false)
        .shadow(true)
        .build()?;
    // Top right of the screen, under the menu bar, where macOS's own mic dot is.
    if let Ok(Some(m)) = w.current_monitor() {
        let s = m.scale_factor();
        let x = m.position().x as f64 / s + m.size().width as f64 / s - 92.0 - 16.0;
        let _ = w.set_position(tauri::LogicalPosition::new(x, m.position().y as f64 / s + 36.0));
    }
    Ok(())
}

/// Once per install: explain, then raise macOS's Accessibility prompt; and the 🌐 setting hint.
#[cfg(target_os = "macos")]
async fn ask_once(app: &AppHandle) {
    let Ok(dir) = app.path().app_data_dir() else { return };
    let marker = dir.join(ASKED_MARKER);
    if marker.exists() {
        return;
    }
    let _ = std::fs::create_dir_all(&dir);
    let _ = std::fs::write(&marker, b"");
    let hint = if mac::globe_does_nothing() {
        String::new()
    } else {
        "\n\nOne more thing: in System Settings › Keyboard, set \"Press 🌐 key to\" to \"Do Nothing\", so a tap \
         talks to Lightbulb instead of opening emoji or switching the input language."
            .into()
    };
    if mac::trusted() {
        if !hint.is_empty() {
            let _ = crate::confirm_native_labelled(app, "Tap 🌐 to talk", hint.trim_start().into(), "OK", "Later").await;
        }
        return;
    }
    let body = format!(
        "Tap the 🌐 (Fn) key to start or stop talking to Lightbulb from any app, or hold it to talk while it's down.\n\n\
         macOS lets an app see the 🌐 key only with Accessibility access. Lightbulb watches that one key; it never \
         reads or records what you type. Until then, ⌃⌥Space does the same.{hint}"
    );
    if crate::confirm_native_labelled(app, "Talk with the 🌐 key", body, "Allow", "Not now").await == Some(true) {
        mac::prompt();
    }
}

#[cfg(target_os = "macos")]
mod mac {
    use super::{send, FN};
    use block2::RcBlock;
    use objc2_app_kit::{NSEvent, NSEventMask, NSEventModifierFlags, NSEventType};
    use objc2_foundation::{NSDictionary, NSNumber, NSString};
    use std::ptr::NonNull;
    use tauri::AppHandle;

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> bool;
        fn AXIsProcessTrustedWithOptions(options: *const std::ffi::c_void) -> bool;
    }

    pub fn trusted() -> bool {
        unsafe { AXIsProcessTrusted() }
    }

    /// macOS's own "allow Lightbulb to control this computer" prompt, which lists the app in
    /// System Settings › Privacy & Security › Accessibility.
    pub fn prompt() {
        let key = NSString::from_str("AXTrustedCheckOptionPrompt");
        let yes = NSNumber::numberWithBool(true);
        let opts = NSDictionary::from_slices(&[&*key], &[&*yes]);
        unsafe { AXIsProcessTrustedWithOptions(objc2::rc::Retained::as_ptr(&opts).cast()) };
    }

    /// System Settings › Keyboard › "Press 🌐 key to": 0 is Do Nothing.
    pub fn globe_does_nothing() -> bool {
        std::process::Command::new("defaults")
            .args(["read", "com.apple.HIToolbox", "AppleFnUsageType"])
            .output()
            .map(|o| String::from_utf8_lossy(&o.stdout).trim() == "0")
            .unwrap_or(false)
    }

    fn on_event(app: &AppHandle, e: &NSEvent) {
        let out = {
            let mut f = FN.lock().unwrap_or_else(|p| p.into_inner());
            match e.r#type() {
                NSEventType::FlagsChanged => {
                    f.flags(e.keyCode(), e.modifierFlags().contains(NSEventModifierFlags::Function))
                }
                NSEventType::KeyDown => f.key(),
                _ => None,
            }
        };
        if let Some(detail) = out {
            send(app, detail);
        }
    }

    /// Main thread only. The monitors live as long as the app.
    pub fn monitor(app: AppHandle, global: bool) {
        let mask = NSEventMask::FlagsChanged | NSEventMask::KeyDown;
        let handle = if global {
            let block = RcBlock::new(move |e: NonNull<NSEvent>| on_event(&app, unsafe { e.as_ref() }));
            NSEvent::addGlobalMonitorForEventsMatchingMask_handler(mask, &block)
        } else {
            let block = RcBlock::new(move |e: NonNull<NSEvent>| -> *mut NSEvent {
                on_event(&app, unsafe { e.as_ref() });
                e.as_ptr() // pass the event on
            });
            unsafe { NSEvent::addLocalMonitorForEventsMatchingMask_handler(mask, &block) }
        };
        std::mem::forget(handle);
    }
}

#[cfg(test)]
mod tests {
    use super::FnKey;

    #[test]
    fn fn_down_up_is_a_press_and_a_release() {
        let mut f = FnKey { down: false, chord: false };
        assert_eq!(f.flags(63, true), Some("down"));
        assert_eq!(f.flags(63, true), None); // a repeat
        assert_eq!(f.flags(63, false), Some("up"));
        assert_eq!(f.flags(56, true), None); // Shift is not the talk key
    }

    #[test]
    fn a_key_while_fn_is_held_cancels_once_and_the_release_says_nothing() {
        let mut f = FnKey { down: false, chord: false };
        assert_eq!(f.key(), None); // typing without Fn
        f.flags(63, true);
        assert_eq!(f.key(), Some("cancel"));
        assert_eq!(f.key(), None);
        assert_eq!(f.flags(63, false), None);
        assert_eq!(f.flags(63, true), Some("down")); // the next press is fresh
    }
}
