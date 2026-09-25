import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { THEME_ATTR, THEME_BOOTSTRAP_SCRIPT, THEME_KEY, setChatTheme, useChatTheme } from "@/hooks/useChatTheme";

function attrs() {
  return { theme: document.documentElement.getAttribute(THEME_ATTR) };
}

describe("useChatTheme", () => {
  beforeEach(() => {
    localStorage.clear();
    // The pre-paint script always runs first in the real app, so the
    // attribute exists before any hook mounts. Reproduce that rather than
    // starting from a bare <html> the app never actually has.
    eval(THEME_BOOTSTRAP_SCRIPT);
  });

  it("defaults to light when nothing is stamped", () => {
    const { result } = renderHook(() => useChatTheme());
    expect(result.current.theme).toBe("light");
  });

  it("reads the attribute the pre-paint script stamped", () => {
    document.documentElement.setAttribute(THEME_ATTR, "dark");
    const { result } = renderHook(() => useChatTheme());
    expect(result.current.theme).toBe("dark");
  });

  it("toggles the theme", () => {
    const { result } = renderHook(() => useChatTheme());

    act(() => result.current.toggleTheme());
    expect(attrs()).toEqual({ theme: "dark" });

    act(() => result.current.toggleTheme());
    expect(attrs()).toEqual({ theme: "light" });
  });

  it("round-trips through localStorage", () => {
    setChatTheme("dark");
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
  });

  it("keeps every consumer in step off one write", () => {
    const a = renderHook(() => useChatTheme());
    const b = renderHook(() => useChatTheme());

    act(() => a.result.current.toggleTheme());

    // The rail toggle and the mobile footer toggle are two mounts of this
    // hook; if they read separate state they'd show opposite icons.
    expect(b.result.current.theme).toBe("dark");
  });

  it("still applies the theme when localStorage throws", () => {
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    try {
      expect(() => setChatTheme("dark")).not.toThrow();
      expect(attrs().theme).toBe("dark");
    } finally {
      spy.mockRestore();
    }
  });
});

/** Stubs `window.matchMedia("(prefers-color-scheme: dark)")` for one test. */
function stubPrefersDark(matches: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({ matches, media: query })),
  );
}

describe("THEME_BOOTSTRAP_SCRIPT", () => {
  beforeEach(() => {
    document.documentElement.removeAttribute(THEME_ATTR);
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("stamps the stored preference", () => {
    localStorage.setItem(THEME_KEY, "dark");
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "dark" });
  });

  it("stamps light when storage is empty and there is no OS preference", () => {
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "light" });
  });

  it("follows the OS dark preference when no theme is stored", () => {
    stubPrefersDark(true);
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "dark" });
  });

  it("follows the OS light preference when no theme is stored", () => {
    stubPrefersDark(false);
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "light" });
  });

  it("a stored theme wins over the OS preference", () => {
    stubPrefersDark(true);
    localStorage.setItem(THEME_KEY, "light");
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "light" });
  });

  it("coerces an unrecognised stored value to the OS/light default", () => {
    localStorage.setItem(THEME_KEY, "solarized");
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "light" });
  });

  it("ignores a leftover isol8:chat-accent value — the accent axis is gone", () => {
    localStorage.setItem("isol8:chat-accent", "full");
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(document.documentElement.hasAttribute("data-chat-accent")).toBe(false);
  });
});
