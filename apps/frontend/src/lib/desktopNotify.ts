// Native OS notifications: the Tauri desktop shell, or the browser
// `Notification` API once the user has granted it. The Rust side initializes
// tauri-plugin-notification and grants `notification:default` to the remote
// origin, and `withGlobalTauri` injects the JS API — this module is the only
// caller.

interface TauriNotificationApi {
  sendNotification: (options: { title: string; body?: string }) => void;
}

function getTauriNotification(): TauriNotificationApi | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tauri = (window as any).__TAURI__;
  return tauri?.notification ?? null;
}

/**
 * Show a native OS notification. Returns true when one was shown, so the
 * caller uses the in-app toast only when it returns false: the desktop app
 * always notifies natively, the web app natively once permission is granted
 * (see `MessageNotifications`' first-gesture `requestPermission()`), and a
 * plain browser without permission gets the toast. Callers decide what counts
 * as "already looking at it"; this never checks focus.
 *
 * Inside Tauri there is no permission step on desktop (the plugin always
 * reports granted; macOS itself decides whether the banner appears) and no
 * click callback, so `opts.onClick`/`opts.tag` are ignored there.
 */
export function nativeNotify(
  title: string,
  body?: string,
  opts?: { onClick?: () => void; tag?: string },
): boolean {
  const notification = getTauriNotification();
  if (notification) {
    try {
      notification.sendNotification({ title, body });
      return true;
    } catch {
      return false;
    }
  }
  try {
    if (!("Notification" in window) || window.Notification.permission !== "granted") return false;
    const browserNotification = new window.Notification(title, { body, tag: opts?.tag });
    browserNotification.onclick = () => {
      window.focus();
      opts?.onClick?.();
      browserNotification.close();
    };
    return true;
  } catch {
    return false;
  }
}
