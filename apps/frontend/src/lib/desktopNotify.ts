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
const SESSION_CLICK = "lightbulb.session.";
// A task question the Mac posted (push source `task_question`): the tag names its thread.
const TASK_CLICK = "task:";
let sessionClick: ((sessionId: string) => void) | null = null;
let taskClick: ((taskId: string) => void) | null = null;

/** Opens the session a Mac request notification is about when its body is clicked. */
export function onSessionNoticeClick(handler: (sessionId: string) => void): () => void {
  sessionClick = handler;
  const tauri = getTauri();
  if (tauri) routeClicks(tauri);
  return () => { if (sessionClick === handler) sessionClick = null; };
}
/** Opens the thread a task-question notification is about when its body is clicked. */
export function onTaskNoticeClick(handler: (taskId: string) => void): () => void {
  taskClick = handler;
  const tauri = getTauri();
  if (tauri) routeClicks(tauri);
  return () => { if (taskClick === handler) taskClick = null; };
}
let routing = false;
function routeClicks(tauri: TauriBridge): void {
  if (routing || !tauri.listen) return;
  routing = true;
  tauri.listen("notification:clicked", (event) => {
    const tag = typeof event.payload === "string" ? event.payload : null;
    const handler = tag === null ? undefined : clickHandlers.get(tag);
    // A request the Mac posted itself (a held message, an agent's ask) names its session.
    if (!handler && tag?.startsWith(SESSION_CLICK)) {
      sessionClick?.(tag.slice(SESSION_CLICK.length));
      return;
    }
    if (!handler && tag?.startsWith(TASK_CLICK)) {
      taskClick?.(tag.slice(TASK_CLICK.length));
      return;
    }
    if (!handler) return;
    clickHandlers.delete(tag as string);
    handler();
  }).catch(() => { routing = false; });
}

/** True inside the Lightbulb desktop app. */
export function isDesktopApp(): boolean {
  return getTauri() !== null;
}

/** The desktop app's paired Mac device id; null outside it, unpaired, or on an older build. */
export async function desktopDeviceId(): Promise<string | null> {
  const tauri = getTauri();
  if (!tauri) return null;
  try {
    const id = await tauri.invoke("desktop_device_id");
    return typeof id === "string" ? id : null;
  } catch {
    return null;
  }
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
  opts?: {
    onClick?: () => void; tag?: string; subtitle?: string; threadId?: string;
    /** Silent banner when false. The browser API has no sound switch; the OS decides there. */
    sound?: boolean;
    /** The desktop app holds the banner back while one of its windows is focused (answering true). */
    onlyUnfocused?: boolean;
  },
): Promise<boolean> {
  const tauri = getTauri();
  if (tauri) {
    if (opts?.tag && opts.onClick) {
      clickHandlers.set(opts.tag, opts.onClick);
      if (clickHandlers.size > 200) clickHandlers.delete(clickHandlers.keys().next().value!);
      routeClicks(tauri);
    }
    try {
      return (await tauri.invoke("native_notify", {
        title, body, tag: opts?.tag, subtitle: opts?.subtitle, threadId: opts?.threadId,
        sound: opts?.sound, onlyUnfocused: opts?.onlyUnfocused,
      })) === true;
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

/** Tag of the desktop app's "open the Lightbulb agent window" ask (agent_chrome.rs SHOW_TAG). */
const AGENT_CHROME_SHOW_TAG = "lightbulb-agent-chrome-show";

/**
 * The desktop app's own notices (the Mac browser's Paperclip notice, "open the agent
 * window") arrive as `desktop:toast` when macOS notifications are off. `show` gets the
 * text and, for the open-the-window ask, an action that raises the agent Chrome.
 * Returns the unsubscribe; a no-op outside the desktop app.
 */
export function onDesktopToast(
  show: (title: string, body: string, open?: () => void) => void,
): () => void {
  const tauri = getTauri();
  if (!tauri?.listen) return () => {};
  let unlisten: (() => void) | null = null;
  let stopped = false;
  tauri.listen("desktop:toast", (event) => {
    const p = event.payload as { title?: unknown; body?: unknown; tag?: unknown } | null;
    if (!p || typeof p.title !== "string" || typeof p.body !== "string") return;
    const open = p.tag === AGENT_CHROME_SHOW_TAG
      ? () => { void tauri.invoke("agent_chrome_raise").catch(() => {}); }
      : undefined;
    show(p.title, p.body, open);
  }).then((stop) => {
    if (stopped) stop();
    else unlisten = stop;
  }).catch(() => {});
  return () => {
    stopped = true;
    unlisten?.();
  };
}
