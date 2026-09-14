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

/**
 * Show a native OS notification. Resolves true when one was shown, so the
 * caller uses the in-app toast only when it resolves false: the desktop app
 * notifies natively (false only for an unbundled dev binary or a denied
 * System Settings permission), the web app natively once permission is
 * granted (see `MessageNotifications`' first-gesture `requestPermission()`),
 * and a plain browser without permission gets the toast. Callers decide what
 * counts as "already looking at it"; this never checks focus.
 */
export async function nativeNotify(
  title: string,
  body?: string,
  opts?: { onClick?: () => void; tag?: string },
): Promise<boolean> {
  const tauri = getTauri();
  if (tauri) {
    if (opts?.tag && opts.onClick) {
      clickHandlers.set(opts.tag, opts.onClick);
      if (clickHandlers.size > 200) clickHandlers.delete(clickHandlers.keys().next().value!);
      routeClicks(tauri);
    }
    try {
      return (await tauri.invoke("native_notify", { title, body, tag: opts?.tag })) === true;
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
