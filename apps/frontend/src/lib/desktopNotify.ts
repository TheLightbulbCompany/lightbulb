// Native OS notifications: the Tauri desktop shell (the Rust `native_notify`
// command over UNUserNotificationCenter — `apps/desktop/src-tauri/src/notify.rs`),
// or the browser `Notification` API once the user has granted it. This module
// is the only caller of either.

type TauriBridge = {
  invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
  listen?: (name: string, handler: (event: { payload: unknown }) => void) => Promise<() => void>;
};

function getTauri(): TauriBridge | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const tauri = (window as any).__TAURI__;
  const invoke = tauri?.core?.invoke;
  return invoke ? { invoke, listen: tauri.event?.listen } : null;
}

// Desktop click routing: the Rust side focuses the window and emits
// `notification:clicked` with the request identifier (our `tag`); the handler
// registered under that tag opens the conversation. One-shot per tag.
const clickHandlers = new Map<string, () => void>();
let routing = false;
function routeClicks(tauri: TauriBridge): void {
  if (routing || !tauri.listen) return;
  routing = true;
  tauri.listen("notification:clicked", (event) => {
    const tag = typeof event.payload === "string" ? event.payload : null;
    const handler = tag === null ? undefined : clickHandlers.get(tag);
    if (!handler) return;
    clickHandlers.delete(tag as string);
    handler();
  }).catch(() => { routing = false; });
}

/** True inside the Lightbulb desktop app. */
export function isDesktopApp(): boolean {
  return getTauri() !== null;
}

/**
 * Native notification permission on this device, in the browser's vocabulary:
 * the desktop app's macOS permission, or the browser's. Null when there is no
 * native path: no Notification API, no app bundle, or a desktop build older
 * than the permission command. With `request` it asks — the browser or macOS
 * prompt while undecided; in the desktop app a "denied" opens Lightbulb's page
 * in System Settings, since macOS never prompts twice. A browser only prompts
 * from a user gesture.
 */
export async function notificationPermission(request = false): Promise<NotificationPermission | null> {
  const tauri = getTauri();
  if (tauri) {
    try {
      const state = await tauri.invoke("notification_permission", { request });
      return state === "granted" || state === "denied" || state === "default" ? state : null;
    } catch {
      return null;
    }
  }
  if (typeof window === "undefined" || !("Notification" in window)) return null;
  if (request && window.Notification.permission === "default") return window.Notification.requestPermission();
  return window.Notification.permission;
}

/**
 * Show a native OS notification. Resolves true when one was shown, so the
 * caller uses the in-app toast only when it resolves false: the desktop app
 * notifies natively once macOS allows it, the web app once the browser does
 * (both asked by `MessageNotifications`), and anything else gets the toast.
 * Callers decide what counts as "already looking at it"; this never checks
 * focus.
 */
export async function nativeNotify(
  title: string,
  body?: string,
  opts?: { onClick?: () => void; tag?: string; subtitle?: string },
): Promise<boolean> {
  const tauri = getTauri();
  if (tauri) {
    if (opts?.tag && opts.onClick) {
      clickHandlers.set(opts.tag, opts.onClick);
      if (clickHandlers.size > 200) clickHandlers.delete(clickHandlers.keys().next().value!);
      routeClicks(tauri);
    }
    try {
      return (await tauri.invoke("native_notify", { title, body, tag: opts?.tag, subtitle: opts?.subtitle })) === true;
    } catch {
      return false;
    }
  }
  try {
    if (!("Notification" in window) || window.Notification.permission !== "granted") return false;
    // The Notification API has no subtitle line; it leads the body instead.
    const browserBody = opts?.subtitle ? (body ? `${opts.subtitle}\n${body}` : opts.subtitle) : body;
    const browserNotification = new window.Notification(title, { body: browserBody, tag: opts?.tag });
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
