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
});
