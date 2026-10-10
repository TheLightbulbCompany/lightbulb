"use client";

export type DesktopPermissionSet = {
  screen: boolean;
  shell: boolean;
  browser: boolean;
  files: boolean;
  /** Paperclip (board) runs may act on this Mac, within the switches above. */
  paperclip: boolean;
};

export type DesktopPermissionGrant = {
  version: 1;
  acceptedAt: string;
  permissions: DesktopPermissionSet;
};

export const DESKTOP_PERMISSIONS_STORAGE_KEY = "isol8.desktop.permissions.v1";
export const DESKTOP_PERMISSIONS_CHANGED_EVENT = "isol8:desktop-permissions-changed";

// Cross-tab storage-event filter: only permission-grant writes (or a full
// localStorage.clear, which fires with key === null) concern the listeners
// in useGateway / DesktopPermissionsGate. Unrelated keys — notably the
// frequently-written workspace layout blob — must not wake them.
export function isPermissionsStorageEvent(event: StorageEvent): boolean {
  return event.key === null || event.key === DESKTOP_PERMISSIONS_STORAGE_KEY;
}

export const DEFAULT_DESKTOP_PERMISSIONS: DesktopPermissionSet = {
  screen: true,
  shell: true,
  browser: true,
  files: true,
  paperclip: true,
};

export const BASIC_DESKTOP_PERMISSIONS: DesktopPermissionSet = {
  screen: false,
  shell: false,
  browser: false,
  files: false,
  paperclip: false,
};

export function readDesktopPermissionGrant(): DesktopPermissionGrant | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(DESKTOP_PERMISSIONS_STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<DesktopPermissionGrant>;
    const permissions = parsed.permissions;
    if (
      parsed.version !== 1 ||
      !permissions ||
      typeof permissions.screen !== "boolean" ||
      typeof permissions.shell !== "boolean" ||
      typeof permissions.browser !== "boolean" ||
      typeof permissions.files !== "boolean"
    ) {
      return null;
    }
    return {
      version: 1,
      acceptedAt:
        typeof parsed.acceptedAt === "string"
          ? parsed.acceptedAt
          : new Date().toISOString(),
      // Grants saved before the Paperclip switch existed: pairing turned it on.
      permissions: {
        screen: permissions.screen,
        shell: permissions.shell,
        browser: permissions.browser,
        files: permissions.files,
        paperclip: typeof permissions.paperclip === "boolean" ? permissions.paperclip : true,
      },
    };
  } catch {
    return null;
  }
}

export async function writeDesktopPermissionGrant(
  permissions: DesktopPermissionSet,
): Promise<DesktopPermissionGrant> {
  // The Mac answers approval requests from its own copy (consent once, at
  // pairing — no per-operation prompt), so every choice is pushed to it FIRST:
  // if the Mac refuses the write this throws and nothing here changes, so a
  // switch can never read off while the Mac still allows it.
  // The Browser switch is the whole setup: the Mac starts its agent Chrome on first use.
  await desktopInvoke()?.("desktop_permissions_set", { permissions });
  const grant: DesktopPermissionGrant = {
    version: 1,
    acceptedAt: new Date().toISOString(),
    permissions,
  };
  window.localStorage.setItem(
    DESKTOP_PERMISSIONS_STORAGE_KEY,
    JSON.stringify(grant),
  );
  window.dispatchEvent(new CustomEvent(DESKTOP_PERMISSIONS_CHANGED_EVENT));
  return grant;
}

/** A desktop command error for people: an older app rejects a command it lacks with
 *  Tauri's raw "Command … not found", which means "update the app". Turning a switch on
 *  or adding a folder asks on the Mac itself; Cancel there is a choice, not a failure. */
export function desktopErrorMessage(error: unknown): string {
  const text = String(error instanceof Error ? error.message : error);
  if (/declined on this Mac/i.test(text)) return "Not changed — you chose Cancel on your Mac.";
  return /command\b.*\bnot found|unknown command/i.test(text)
    ? "Update the Lightbulb desktop app, then reopen Settings."
    : text;
}

type Invoke = <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
function desktopInvoke(): Invoke | undefined {
  return (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
}

/** What the Mac itself will allow; null outside the desktop app or on an older build. */
export async function readDesktopPermissionsFromDesktop(): Promise<DesktopPermissionSet | null> {
  const invoke = desktopInvoke();
  if (!invoke) return null;
  try {
    const s = await invoke<DesktopPermissionSet>("desktop_permissions_get");
    return { screen: s.screen, shell: s.shell, browser: s.browser, files: s.files, paperclip: s.paperclip };
  } catch {
    return null;
  }
}
