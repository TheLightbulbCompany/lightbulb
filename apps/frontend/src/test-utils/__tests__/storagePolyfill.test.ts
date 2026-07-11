import { describe, it, expect } from "vitest";
import { installStoragePolyfill } from "../storagePolyfill";

describe("installStoragePolyfill", () => {
  it("provides working local/session storage and restores on cleanup", () => {
    const restore = installStoragePolyfill();
    window.localStorage.setItem("k", "v");
    expect(window.localStorage.getItem("k")).toBe("v");
    window.sessionStorage.setItem("s", "1");
    expect(window.sessionStorage.getItem("s")).toBe("1");
    window.localStorage.removeItem("k");
    expect(window.localStorage.getItem("k")).toBeNull();
    window.localStorage.setItem("a", "b");
    window.localStorage.clear();
    expect(window.localStorage.getItem("a")).toBeNull();
    restore();
  });

  it("restore() drops the polyfill instances from every target it installed on", () => {
    const restore = installStoragePolyfill();
    const polyLocal = window.localStorage;
    const polySession = window.sessionStorage;
    // Sanity: the polyfill really is what's installed pre-restore.
    expect(globalThis.localStorage).toBe(polyLocal);
    expect(globalThis.sessionStorage).toBe(polySession);
    restore();
    // Identity assertion via own-property descriptors rather than property
    // reads — a restored native accessor may throw when invoked in some
    // environments. The Map-backed instances captured above must no longer
    // be reachable as an own value on either globalThis or window.
    for (const target of [globalThis, window] as object[]) {
      const localDesc = Object.getOwnPropertyDescriptor(target, "localStorage");
      const sessionDesc = Object.getOwnPropertyDescriptor(target, "sessionStorage");
      expect(localDesc?.value).not.toBe(polyLocal);
      expect(sessionDesc?.value).not.toBe(polySession);
    }
  });
});
