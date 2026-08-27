"use client";

export type DesktopPermissionSet = {
  screen: boolean;
  shell: boolean;
  browser: boolean;
  files: boolean;
};

export type DesktopPermissionGrant = {
  version: 1;
  acceptedAt: string;
  permissions: DesktopPermissionSet;
};

// Renamed from `isol8.*` at the Lightbulb AI rebrand — desktop folder grants
// are re-asked once. The event name is same-document only, so both sides flip
// together.
export const DESKTOP_PERMISSIONS_STORAGE_KEY = "lightbulb.desktop.permissions.v1";
export const DESKTOP_PERMISSIONS_CHANGED_EVENT = "lightbulb:desktop-permissions-changed";

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
};

export const BASIC_DESKTOP_PERMISSIONS: DesktopPermissionSet = {
  screen: false,
  shell: false,
  browser: false,
  files: false,
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
      permissions,
    };
  } catch {
    return null;
  }
}

export function writeDesktopPermissionGrant(
  permissions: DesktopPermissionSet,
): DesktopPermissionGrant {
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
