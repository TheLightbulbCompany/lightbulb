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
//!
//! A held message or an agent's ask (Task 7b) is a notification of the `REQUEST_CATEGORY`
//! that carries its whole decision in `userInfo`. Whichever process macOS hands the Allow or
//! Decline to, the window or `serve`, sends that decision itself
//! (`local_sessions::held_notice::on_action`): nothing is forwarded between processes. That
//! core needs no Tauri: `serve` installs it with `install_headless`.

use std::collections::BTreeMap;
use tauri::AppHandle;

/// The category of a request notification, and its two actions.
pub const REQUEST_CATEGORY: &str = "lightbulb.request";
pub const ALLOW: &str = "allow";
pub const DECLINE: &str = "decline";

/// One request notification: `id` is its identifier, which a repeat replaces. `info` is its
/// `userInfo`: everything an Allow or Decline needs to be sent from any process.
#[derive(Clone, Debug, PartialEq)]
pub struct Notice {
    pub id: String,
    pub title: String,
    pub body: String,
    pub info: BTreeMap<String, String>,
}

/// What posts and withdraws request notifications; tests use their own.
pub trait Center: Send {
    fn post(&self, notice: &Notice);
    fn withdraw(&self, ids: &[String]);
}

/// The real `UNUserNotificationCenter`. Not allowed (or no app bundle) shows nothing and
/// changes nothing else: the band, the phone and the web still answer.
pub struct Native;

impl Center for Native {
    fn post(&self, notice: &Notice) {
        #[cfg(target_os = "macos")]
        drop(macos::schedule(&notice.title, Some(&notice.body), None, None, &notice.id, Some((REQUEST_CATEGORY, &notice.info))));
        #[cfg(not(target_os = "macos"))]
        let _ = notice;
    }
    fn withdraw(&self, ids: &[String]) {
        #[cfg(target_os = "macos")]
        macos::withdraw(ids);
        #[cfg(not(target_os = "macos"))]
        let _ = ids;
    }
}

/// A press on a request notification: Allow or Decline is answered from this process, with
/// the decision the notification carries. Anything else (a plain click) is not a decision.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn act(id: String, action: &str, title: String, info: BTreeMap<String, String>) -> bool {
    if action != ALLOW && action != DECLINE {
        return false;
    }
    crate::local_sessions::held_notice::on_action(Notice { id, title, body: String::new(), info }, action.to_owned());
    true
}

pub fn install(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    macos::install(Some(app));
    #[cfg(not(target_os = "macos"))]
    let _ = app;
}

/// `serve`: the same delegate and category with no window. A click opens nothing; Allow and
/// Decline are still answered.
pub fn install_headless() {
    #[cfg(target_os = "macos")]
    macos::install(None);
}

/// `lightbulb join`: raise the macOS prompt once, waiting up to `wait` for the answer. A no-op
/// once someone has answered it, and outside an app bundle.
pub fn ask_once(wait: std::time::Duration) {
    #[cfg(target_os = "macos")]
    tauri::async_runtime::block_on(async {
        let _ = tokio::time::timeout(wait, notification_permission(true)).await;
    });
    #[cfg(not(target_os = "macos"))]
    let _ = wait;
}

/// Shows a native notification; `true` once the OS accepted it with permission
/// to show it. `tag` is the request identifier (a repeat replaces the earlier
/// banner, like the browser API) and comes back on `notification:clicked`.
/// `subtitle` and `thread_id` mirror the phone push: the conversation kind line,
/// and the key macOS groups one conversation's banners under.
#[tauri::command]
pub async fn native_notify(
    title: String,
    body: Option<String>,
    tag: Option<String>,
    subtitle: Option<String>,
    thread_id: Option<String>,
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
            None,
        ) {
            Some(accepted) => accepted.await.unwrap_or(false),
            None => false,
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (title, body, tag, subtitle, thread_id);
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
    use objc2::runtime::{AnyObject, Bool, ProtocolObject};
    use objc2::{define_class, msg_send, AnyThread, DefinedClass};
    use objc2_foundation::{NSArray, NSBundle, NSDictionary, NSError, NSObject, NSObjectProtocol, NSSet, NSString};
    use std::collections::BTreeMap;
    use objc2_user_notifications::{
        UNAuthorizationOptions, UNAuthorizationStatus, UNMutableNotificationContent,
        UNNotification, UNNotificationAction, UNNotificationActionOptions,
        UNNotificationCategory, UNNotificationCategoryOptions, UNNotificationPresentationOptions,
        UNNotificationRequest, UNNotificationResponse, UNNotificationSettings, UNNotificationSound,
        UNUserNotificationCenter, UNUserNotificationCenterDelegate,
    };
    use tauri::{AppHandle, Emitter};
    use tokio::sync::oneshot;

    use crate::log;

    struct Ivars {
        /// None under `serve`: no window to focus or webview to tell.
        app: Option<AppHandle>,
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
                let request = response.notification().request();
                let identifier = request.identifier().to_string();
                let content = request.content();
                if super::act(identifier.clone(), &response.actionIdentifier().to_string(), content.title().to_string(), strings(&content.userInfo())) {
                    completion.call(());
                    return;
                }
                let Some(app) = self.ivars().app.clone() else {
                    completion.call(());
                    return;
                };
                if identifier == crate::agent_chrome::SHOW_TAG {
                    // "Open the Lightbulb agent window": the click made Lightbulb active,
                    // so it can hand activation to the agent Chrome.
                    use tauri::Manager;
                    if let Ok(app_data) = app.path().app_data_dir() {
                        let _ = app.run_on_main_thread(move || crate::agent_chrome::raise_from_click(&app_data));
                    }
                    completion.call(());
                    return;
                }
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

    pub fn install(app: Option<&AppHandle>) {
        if !bundled() {
            log("[notify] not running from an app bundle; native notifications off");
            return;
        }
        let delegate = Delegate::alloc().set_ivars(Ivars { app: app.cloned() });
        let delegate: Retained<Delegate> = unsafe { msg_send![super(delegate), init] };
        let center = UNUserNotificationCenter::currentNotificationCenter();
        center.setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
        // Allow and Decline: neither asks to unlock, as the decision route is device-signed.
        let action = |id: &str, title: &str, options| {
            UNNotificationAction::actionWithIdentifier_title_options(&NSString::from_str(id), &NSString::from_str(title), options)
        };
        let actions = NSArray::from_retained_slice(&[
            action(super::ALLOW, "Allow", UNNotificationActionOptions::empty()),
            action(super::DECLINE, "Decline", UNNotificationActionOptions::Destructive),
        ]);
        let category = UNNotificationCategory::categoryWithIdentifier_actions_intentIdentifiers_options(
            &NSString::from_str(super::REQUEST_CATEGORY),
            &actions,
            &NSArray::new(),
            UNNotificationCategoryOptions::empty(),
        );
        center.setNotificationCategories(&NSSet::from_retained_slice(&[category]));
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
        category: Option<(&str, &BTreeMap<String, String>)>,
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
        if let Some((category, info)) = category {
            content.setCategoryIdentifier(&NSString::from_str(category));
            let keys: Vec<Retained<NSString>> = info.keys().map(|k| NSString::from_str(k)).collect();
            let values: Vec<Retained<AnyObject>> = info.values().map(|v| NSString::from_str(v).into_super().into_super()).collect();
            let keys: Vec<&NSString> = keys.iter().map(|k| &**k).collect();
            let dict: Retained<NSDictionary<NSString, AnyObject>> = NSDictionary::from_retained_objects(&keys, &values);
            // SAFETY: a dictionary of strings is a valid property list, which userInfo must be.
            unsafe { content.setUserInfo(&*(Retained::as_ptr(&dict) as *const NSDictionary)) };
        }
        content.setSound(Some(&UNNotificationSound::defaultSound()));
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

    /// A notification's `userInfo` string pairs; anything else in it is skipped.
    fn strings(info: &NSDictionary) -> BTreeMap<String, String> {
        let (keys, values) = info.to_vecs();
        keys.iter()
            .zip(values.iter())
            .filter_map(|(k, v)| Some((k.downcast_ref::<NSString>()?.to_string(), v.downcast_ref::<NSString>()?.to_string())))
            .collect()
    }

    /// Takes these banners out of Notification Center.
    pub fn withdraw(ids: &[String]) {
        if !bundled() || ids.is_empty() {
            return;
        }
        let ids: Vec<Retained<NSString>> = ids.iter().map(|id| NSString::from_str(id)).collect();
        UNUserNotificationCenter::currentNotificationCenter()
            .removeDeliveredNotificationsWithIdentifiers(&NSArray::from_retained_slice(&ids));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Only Allow and Decline are decisions (an Allow here would send one, so tests never press
    /// it: `held_notice` covers that path against a stub server).
    #[test]
    fn notify_a_plain_click_is_not_a_decision() {
        let info = BTreeMap::new();
        assert!(!act("lightbulb.request.r1".into(), "com.apple.UNNotificationDefaultActionIdentifier", String::new(), info.clone()));
        assert!(!act("lightbulb.request.r1".into(), "com.apple.UNNotificationDismissActionIdentifier", String::new(), info));
        assert_eq!((REQUEST_CATEGORY, ALLOW, DECLINE), ("lightbulb.request", "allow", "decline"));
    }
}
