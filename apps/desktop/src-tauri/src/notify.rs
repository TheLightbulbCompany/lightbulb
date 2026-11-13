//! Native macOS notifications through `UNUserNotificationCenter`.
//!
//! `tauri-plugin-notification` posts through `NSUserNotificationCenter`
//! (deprecated since macOS 11) behind an `NSBundle.bundleIdentifier` swizzle;
//! on a current macOS the app never registered under System Settings →
//! Notifications and no banner ever appeared. This is the replacement: a
//! delegate that keeps banners visible while the window is frontmost — the
//! webview already decides what counts as "already looking at it" — a click
//! that focuses the window and hands the notification's identifier back to the
//! webview as `notification:clicked`, and `notification_permission`, which the
//! webview calls once someone has signed in. Launch only registers the app
//! provisionally, which lists it in System Settings without a prompt; the macOS
//! prompt appears once, so it waits for the ask after sign-in.
//!
//! `native_notify` answers `false` whenever macOS would not show the banner —
//! not allowed yet, turned off in System Settings, or a bare `tauri dev` binary
//! with no bundle id — so the webview falls back to its in-app toast. macOS
//! itself accepts a request from an app that is switched off and then presents
//! it as nothing, so the permission is checked before every request.

use tauri::AppHandle;

pub fn install(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    macos::install(app);
    #[cfg(not(target_os = "macos"))]
    let _ = app;
}

/// The "only when Lightbulb isn't focused" setting: the banner is held back while one of the
/// app's windows is focused.
pub(crate) fn held_for_focus(only_unfocused: bool, app_focused: bool) -> bool {
    only_unfocused && app_focused
}

/// The webview's banner: `post`, unless `only_unfocused` (the person's "only when Lightbulb isn't
/// focused") and one of the app's windows is focused. A held-back banner answers `true` — handled
/// on purpose — so the webview does not fall back to a toast. `sound` false is a silent banner; an
/// older webview sends neither new field and gets today's banner with sound.
#[tauri::command]
#[allow(clippy::too_many_arguments)]
pub async fn native_notify(
    app: AppHandle,
    title: String,
    body: Option<String>,
    tag: Option<String>,
    subtitle: Option<String>,
    thread_id: Option<String>,
    sound: Option<bool>,
    only_unfocused: Option<bool>,
) -> bool {
    use tauri::Manager;
    let focused = app.webview_windows().values().any(|window| window.is_focused().unwrap_or(false));
    if held_for_focus(only_unfocused.unwrap_or(false), focused) {
        return true;
    }
    post(title, body, tag, subtitle, thread_id, sound.unwrap_or(true)).await
}

/// Shows a native notification; `true` once the OS accepted it with permission
/// to show it. `tag` is the request identifier (a repeat replaces the earlier
/// banner, like the browser API) and comes back on `notification:clicked`.
/// `subtitle` and `thread_id` mirror the phone push: the conversation kind line,
/// and the key macOS groups one conversation's banners under.
async fn post(
    title: String,
    body: Option<String>,
    tag: Option<String>,
    subtitle: Option<String>,
    thread_id: Option<String>,
    sound: bool,
) -> bool {
    #[cfg(target_os = "macos")]
    {
        if notification_permission(false).await != "granted" {
            return false;
        }
        let identifier = tag.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        match macos::schedule(
            &title,
            body.as_deref(),
            subtitle.as_deref(),
            thread_id.as_deref(),
            &identifier,
            sound,
        ) {
            Some(accepted) => accepted.await.unwrap_or(false),
            None => false,
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (title, body, tag, subtitle, thread_id, sound);
        false
    }
}

/// Notification permission in the browser's vocabulary, so the webview treats
/// the desktop app and a browser alike: "granted", "denied", "default" (not
/// asked yet) or "unsupported" (no app bundle). With `request` it raises the
/// macOS prompt, which macOS shows only while nobody has answered it. After a
/// "denied" the only way back is System Settings, where the provisional
/// registration at launch keeps Lightbulb listed.
#[tauri::command]
pub async fn notification_permission(request: bool) -> &'static str {
    #[cfg(target_os = "macos")]
    {
        use objc2_user_notifications::UNAuthorizationStatus as Status;
        let Some(status) = macos::status() else {
            return "unsupported";
        };
        match status.await.unwrap_or(Status::NotDetermined) {
            Status::Authorized | Status::Ephemeral => return "granted",
            // Provisional is the silent launch registration: listed in System
            // Settings but quiet, so the real prompt is still to come.
            Status::NotDetermined | Status::Provisional => {}
            _ => return "denied",
        }
        if !request {
            return "default";
        }
        if macos::request(macos::PROMPT).await.unwrap_or(false) {
            "granted"
        } else {
            "denied"
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = request;
        "unsupported"
    }
}

#[cfg(target_os = "macos")]
mod macos {
    use std::ptr::NonNull;
    use std::sync::Mutex;

    use block2::{DynBlock, RcBlock};
    use objc2::rc::Retained;
    use objc2::runtime::{Bool, ProtocolObject};
    use objc2::{define_class, msg_send, AnyThread, DefinedClass};
    use objc2_foundation::{NSBundle, NSError, NSObject, NSObjectProtocol, NSString};
    use objc2_user_notifications::{
        UNAuthorizationOptions, UNAuthorizationStatus, UNMutableNotificationContent,
        UNNotification, UNNotificationPresentationOptions, UNNotificationRequest,
        UNNotificationResponse, UNNotificationSettings, UNNotificationSound,
        UNUserNotificationCenter, UNUserNotificationCenterDelegate,
    };
    use tauri::{AppHandle, Emitter};
    use tokio::sync::oneshot;

    use crate::log;

    struct Ivars {
        app: AppHandle,
    }

    define_class!(
        // SAFETY: NSObject has no subclassing requirements; Delegate has no Drop.
        #[unsafe(super(NSObject))]
        #[name = "LightbulbNotificationDelegate"]
        #[ivars = Ivars]
        struct Delegate;

        unsafe impl NSObjectProtocol for Delegate {}

        unsafe impl UNUserNotificationCenterDelegate for Delegate {
            // Banner even while Lightbulb is frontmost: without a delegate macOS
            // drops foreground notifications, and the webview has already
            // suppressed the "you are looking at this conversation" case.
            #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
            fn will_present(
                &self,
                _center: &UNUserNotificationCenter,
                _notification: &UNNotification,
                completion: &DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
            ) {
                completion.call((UNNotificationPresentationOptions::Banner
                    | UNNotificationPresentationOptions::Sound,));
            }

            #[unsafe(method(userNotificationCenter:didReceiveNotificationResponse:withCompletionHandler:))]
            fn did_receive(
                &self,
                _center: &UNUserNotificationCenter,
                response: &UNNotificationResponse,
                completion: &DynBlock<dyn Fn()>,
            ) {
                let identifier = response.notification().request().identifier().to_string();
                let app = self.ivars().app.clone();
                let focus = app.clone();
                let _ =
                    app.run_on_main_thread(move || crate::focus_or_recreate_main_window(&focus));
                let _ = app.emit("notification:clicked", identifier);
                completion.call(());
            }
        }
    );

    /// `UNUserNotificationCenter` raises an ObjC exception (not an error) when
    /// the process has no bundle, which a bare `tauri dev` binary does not.
    fn bundled() -> bool {
        NSBundle::mainBundle().bundleIdentifier().is_some()
    }

    fn describe(error: *mut NSError) -> String {
        // SAFETY: a non-null pointer from the framework is a live NSError for
        // the duration of the completion callback.
        unsafe { error.as_ref() }
            .map(|error| error.localizedDescription().to_string())
            .unwrap_or_default()
    }

    pub fn install(app: &AppHandle) {
        if !bundled() {
            log("[notify] not running from an app bundle; native notifications off");
            return;
        }
        let delegate = Delegate::alloc().set_ivars(Ivars { app: app.clone() });
        let delegate: Retained<Delegate> = unsafe { msg_send![super(delegate), init] };
        let center = UNUserNotificationCenter::currentNotificationCenter();
        center.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
        // `delegate` is a weak property on the center; ours lives as long as the process.
        std::mem::forget(delegate);
        // Provisional authorization shows no prompt but lists Lightbulb under
        // System Settings → Notifications from first launch, so it can always
        // be switched on there. A no-op once someone has answered the prompt.
        drop(request(UNAuthorizationOptions::Provisional)); // answer is only logged
    }

    /// What the after-sign-in ask requests: the options that raise the prompt.
    pub const PROMPT: UNAuthorizationOptions = UNAuthorizationOptions::Alert
        .union(UNAuthorizationOptions::Sound)
        .union(UNAuthorizationOptions::Badge);

    /// The app's authorization status; `None` outside an app bundle.
    pub fn status() -> Option<oneshot::Receiver<UNAuthorizationStatus>> {
        if !bundled() {
            return None;
        }
        let (tx, rx) = oneshot::channel();
        let tx = Mutex::new(Some(tx));
        let done = RcBlock::new(move |settings: NonNull<UNNotificationSettings>| {
            // SAFETY: the framework passes a live settings object for the
            // duration of the completion callback.
            let status = unsafe { settings.as_ref() }.authorizationStatus();
            if let Some(tx) = tx.lock().ok().and_then(|mut slot| slot.take()) {
                let _ = tx.send(status);
            }
        });
        UNUserNotificationCenter::currentNotificationCenter()
            .getNotificationSettingsWithCompletionHandler(&done);
        Some(rx)
    }

    /// Requests `options`, resolving with the answer: `PROMPT` shows the macOS
    /// prompt while nobody has answered it (provisional included).
    pub fn request(options: UNAuthorizationOptions) -> oneshot::Receiver<bool> {
        let (tx, rx) = oneshot::channel();
        let tx = Mutex::new(Some(tx));
        let done = RcBlock::new(move |granted: Bool, error: *mut NSError| {
            log(&format!(
                "[notify] authorization {options:?} granted={} {}",
                granted.as_bool(),
                describe(error)
            ));
            if let Some(tx) = tx.lock().ok().and_then(|mut slot| slot.take()) {
                let _ = tx.send(granted.as_bool());
            }
        });
        UNUserNotificationCenter::currentNotificationCenter()
            .requestAuthorizationWithOptions_completionHandler(options, &done);
        rx
    }

    /// Hands the request to the center; the receiver resolves with whether it
    /// was accepted. Acceptance says nothing about display: macOS accepts from
    /// an app that is switched off, which is why `native_notify` asks first.
    pub fn schedule(
        title: &str,
        body: Option<&str>,
        subtitle: Option<&str>,
        thread_id: Option<&str>,
        identifier: &str,
        sound: bool,
    ) -> Option<oneshot::Receiver<bool>> {
        if !bundled() {
            return None;
        }
        let content = UNMutableNotificationContent::new();
        content.setTitle(&NSString::from_str(title));
        if let Some(body) = body {
            content.setBody(&NSString::from_str(body));
        }
        if let Some(subtitle) = subtitle {
            content.setSubtitle(&NSString::from_str(subtitle));
        }
        if let Some(thread_id) = thread_id {
            content.setThreadIdentifier(&NSString::from_str(thread_id));
        }
        if sound {
            content.setSound(Some(&UNNotificationSound::defaultSound()));
        }
        let request = UNNotificationRequest::requestWithIdentifier_content_trigger(
            &NSString::from_str(identifier),
            &content,
            None,
        );
        let (tx, rx) = oneshot::channel();
        let tx = Mutex::new(Some(tx));
        let done = RcBlock::new(move |error: *mut NSError| {
            if !error.is_null() {
                log(&format!("[notify] add failed: {}", describe(error)));
            }
            if let Some(tx) = tx.lock().ok().and_then(|mut slot| slot.take()) {
                let _ = tx.send(error.is_null());
            }
        });
        UNUserNotificationCenter::currentNotificationCenter()
            .addNotificationRequest_withCompletionHandler(&request, Some(&done));
        Some(rx)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// "Only when Lightbulb isn't focused" holds a banner back only while the app is focused.
    #[test]
    fn notify_focus_gate() {
        assert!(held_for_focus(true, true));
        assert!(!held_for_focus(true, false));
        assert!(!held_for_focus(false, true));
        assert!(!held_for_focus(false, false));
    }
}
