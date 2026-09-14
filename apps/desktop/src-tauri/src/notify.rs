//! Native macOS notifications through `UNUserNotificationCenter`.
//!
//! `tauri-plugin-notification` posts through `NSUserNotificationCenter`
//! (deprecated since macOS 11) behind an `NSBundle.bundleIdentifier` swizzle;
//! on a current macOS the app never registered under System Settings →
//! Notifications and no banner ever appeared. This is the replacement: one
//! authorization request at launch (which is what registers the app), a
//! delegate that keeps banners visible while the window is frontmost — the
//! webview already decides what counts as "already looking at it" — and a
//! click that focuses the window and hands the notification's identifier back
//! to the webview as `notification:clicked`.
//!
//! `UNUserNotificationCenter` only works from a real `.app` bundle. A bare
//! `tauri dev` binary has no bundle id, so `native_notify` answers `false`
//! there and the webview falls back to its in-app toast.

use tauri::AppHandle;

pub fn install(app: &AppHandle) {
    #[cfg(target_os = "macos")]
    macos::install(app);
    #[cfg(not(target_os = "macos"))]
    let _ = app;
}

/// Shows a native notification; `true` once the OS accepted it. `tag` is the
/// request identifier (a repeat replaces the earlier banner, like the browser
/// API) and comes back on `notification:clicked`.
#[tauri::command]
pub async fn native_notify(title: String, body: Option<String>, tag: Option<String>) -> bool {
    #[cfg(target_os = "macos")]
    {
        let identifier = tag.unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
        match macos::schedule(&title, body.as_deref(), &identifier) {
            Some(accepted) => accepted.await.unwrap_or(false),
            None => false,
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (title, body, tag);
        false
    }
}

#[cfg(target_os = "macos")]
mod macos {
    use std::sync::Mutex;

    use block2::{DynBlock, RcBlock};
    use objc2::rc::Retained;
    use objc2::runtime::{Bool, ProtocolObject};
    use objc2::{define_class, msg_send, AnyThread, DefinedClass};
    use objc2_foundation::{NSBundle, NSError, NSObject, NSObjectProtocol, NSString};
    use objc2_user_notifications::{
        UNAuthorizationOptions, UNMutableNotificationContent, UNNotification,
        UNNotificationPresentationOptions, UNNotificationRequest, UNNotificationResponse,
        UNNotificationSound, UNUserNotificationCenter, UNUserNotificationCenterDelegate,
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
                let _ = app.run_on_main_thread(move || crate::focus_or_recreate_main_window(&focus));
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
        let done = RcBlock::new(|granted: Bool, error: *mut NSError| {
            log(&format!(
                "[notify] authorization granted={} {}",
                granted.as_bool(),
                describe(error)
            ));
        });
        center.requestAuthorizationWithOptions_completionHandler(
            UNAuthorizationOptions::Alert | UNAuthorizationOptions::Sound | UNAuthorizationOptions::Badge,
            &done,
        );
    }

    /// Hands the request to the center; the receiver resolves with whether it
    /// was accepted (denied permission answers `false` here, not with a throw).
    pub fn schedule(title: &str, body: Option<&str>, identifier: &str) -> Option<oneshot::Receiver<bool>> {
        if !bundled() {
            return None;
        }
        let content = UNMutableNotificationContent::new();
        content.setTitle(&NSString::from_str(title));
        if let Some(body) = body {
            content.setBody(&NSString::from_str(body));
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
}
