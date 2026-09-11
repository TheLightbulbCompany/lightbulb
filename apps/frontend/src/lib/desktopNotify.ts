// Native OS notifications for the Tauri desktop shell. No-op in a plain
// browser. The Rust side already initializes tauri-plugin-notification and
// grants `notification:default` to the remote origin, and `withGlobalTauri`
// injects the JS API — this module is the only caller.

interface TauriNotificationApi {
  isPermissionGranted: () => Promise<boolean>;
  requestPermission: () => Promise<string>;
  sendNotification: (options: { title: string; body?: string }) => void;
}

function getTauriNotification(): TauriNotificationApi | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tauri = (window as any).__TAURI__;
  return tauri?.notification ?? null;
}

/**
 * Mirror an in-app toast as a native OS notification when the window is not
 * focused (a focused window already shows the toast). Best-effort: any
 * failure is swallowed.
 *
 * Inside the Tauri desktop shell this is unchanged (Tauri v2's notification
 * API gives no click callback, so `opts.onClick`/`opts.tag` are ignored
 * there). Outside Tauri it falls back to the browser `Notification` API,
 * which requires permission to already be granted — see
 * `MessageNotifications`'s first-gesture `requestPermission()` call.
 */
export function nativeNotify(
  title: string,
  body?: string,
  opts?: { onClick?: () => void; tag?: string },
): void {
  if (document.hasFocus()) return;
  const notification = getTauriNotification();
  if (notification) {
    void (async () => {
      try {
        let granted = await notification.isPermissionGranted();
        if (!granted) {
          granted = (await notification.requestPermission()) === "granted";
        }
        if (granted) notification.sendNotification({ title, body });
      } catch {
        // Never let native-notify failures surface into the app.
      }
    })();
    return;
  }
  try {
    if (!("Notification" in window) || window.Notification.permission !== "granted") return;
    const browserNotification = new window.Notification(title, { body, tag: opts?.tag });
    browserNotification.onclick = () => {
      window.focus();
      opts?.onClick?.();
      browserNotification.close();
    };
  } catch {
    // Best-effort — never let native-notify failures surface into the app.
  }
}
