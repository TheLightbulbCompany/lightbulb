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

  it("writes, reads, and announces a valid grant", async () => {
    const listener = vi.fn();
    window.addEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, listener);

    const grant = await writeDesktopPermissionGrant({
      screen: false,
      shell: true,
      browser: true,
      files: false,
      paperclip: false,
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
        paperclip: false,
      },
    });

    window.removeEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, listener);
  });

  describe("in the desktop app", () => {
    const ON = { screen: true, shell: true, browser: true, files: true, paperclip: true };
    const SHELL_OFF = { ...ON, shell: false };
    const invoke = vi.fn();
    beforeEach(() => {
      invoke.mockReset();
      Object.assign(window, { __TAURI__: { core: { invoke } } });
      return () => { delete (window as { __TAURI__?: unknown }).__TAURI__; };
    });

    it("leaves the stored grant alone and throws when the Mac refuses the write", async () => {
      invoke.mockResolvedValue(undefined);
      await writeDesktopPermissionGrant(ON);
      const listener = vi.fn();
      window.addEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, listener);
      invoke.mockRejectedValue("write desktop_permissions.json: Permission denied");

      await expect(writeDesktopPermissionGrant(SHELL_OFF)).rejects.toBe("write desktop_permissions.json: Permission denied");

      // The Mac still allows Shell, so this window must not say it is off.
      expect(readDesktopPermissionGrant()?.permissions.shell).toBe(true);
      expect(listener).not.toHaveBeenCalled();
      window.removeEventListener(DESKTOP_PERMISSIONS_CHANGED_EVENT, listener);
    });

    it("stores the grant once the Mac has saved it", async () => {
      invoke.mockResolvedValue(undefined);
      await writeDesktopPermissionGrant(SHELL_OFF);
      expect(invoke).toHaveBeenCalledWith("desktop_permissions_set", { permissions: SHELL_OFF });
      expect(readDesktopPermissionGrant()?.permissions.shell).toBe(false);
    });
  });

  it("reads a grant saved before the Paperclip switch as Paperclip on", () => {
    window.localStorage.setItem(
      DESKTOP_PERMISSIONS_STORAGE_KEY,
      JSON.stringify({ version: 1, permissions: { screen: true, shell: true, browser: true, files: true } }),
    );
    expect(readDesktopPermissionGrant()?.permissions.paperclip).toBe(true);
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
