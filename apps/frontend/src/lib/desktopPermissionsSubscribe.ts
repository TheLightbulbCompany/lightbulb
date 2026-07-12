// Shared cross-tab + same-tab subscription to desktop-permission changes.
// The two-listener pattern (custom event + key-filtered storage event) was
// duplicated in useGateway and DesktopPermissionsGate; PR-2's layout blob
// makes an unfiltered listener expensive, so consumers must go through here.
import {
  DESKTOP_PERMISSIONS_CHANGED_EVENT,
  isPermissionsStorageEvent,
} from "./desktopPermissions";

export function subscribeDesktopPermissions(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  const onStorage = (event: StorageEvent) => {
    if (isPermissionsStorageEvent(event)) onChange();
  };
  window.addEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, onChange);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, onChange);
    window.removeEventListener("storage", onStorage);
  };
}
