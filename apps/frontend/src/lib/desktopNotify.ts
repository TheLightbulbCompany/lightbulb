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
 * Mirror an in-app toast as a native OS notification when running inside the
 * desktop shell and the window is not focused (a focused window already shows
 * the toast). Best-effort: any failure is swallowed.
 */
export function nativeNotify(title: string, body?: string): void {
  const notification = getTauriNotification();
  if (!notification || document.hasFocus()) return;
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
}
