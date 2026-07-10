// @vitest-environment jsdom

import { describe, expect, it, vi, beforeEach } from "vitest";

import {
  DESKTOP_PERMISSIONS_CHANGED_EVENT,
  DESKTOP_PERMISSIONS_STORAGE_KEY,
  readDesktopPermissionGrant,
  writeDesktopPermissionGrant,
} from "../desktopPermissions";

describe("desktopPermissions", () => {
  const store = new Map<string, string>();

  beforeEach(() => {
    store.clear();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        clear: () => store.clear(),
        getItem: (key: string) => store.get(key) ?? null,
        removeItem: (key: string) => store.delete(key),
        setItem: (key: string, value: string) => store.set(key, value),
      },
    });
  });

  it("returns null when no grant is stored", () => {
    expect(readDesktopPermissionGrant()).toBeNull();
  });

  it("writes, reads, and announces a valid grant", () => {
    const listener = vi.fn();
    window.addEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, listener);

    const grant = writeDesktopPermissionGrant({
      screen: false,
      shell: true,
      browser: true,
      files: false,
    });

    expect(listener).toHaveBeenCalledTimes(1);
    expect(grant.version).toBe(1);
    expect(readDesktopPermissionGrant()).toMatchObject({
      version: 1,
      permissions: {
        screen: false,
        shell: true,
        browser: true,
        files: false,
      },
    });

    window.removeEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, listener);
  });

  it("fails closed on malformed or partial stored grants", () => {
    window.localStorage.setItem(DESKTOP_PERMISSIONS_STORAGE_KEY, "{");
    expect(readDesktopPermissionGrant()).toBeNull();

    window.localStorage.setItem(
      DESKTOP_PERMISSIONS_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        permissions: {
          shell: true,
          browser: true,
          files: true,
        },
      }),
    );
    expect(readDesktopPermissionGrant()).toBeNull();
  });
});
