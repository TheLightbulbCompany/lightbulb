import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ACCENT_ATTR,
  ACCENT_KEY,
  THEME_ATTR,
  THEME_BOOTSTRAP_SCRIPT,
  THEME_KEY,
  setChatAccent,
  setChatTheme,
  useChatTheme,
} from "@/hooks/useChatTheme";

function attrs() {
  return {
    theme: document.documentElement.getAttribute(THEME_ATTR),
    accent: document.documentElement.getAttribute(ACCENT_ATTR),
  };
}

describe("useChatTheme", () => {
  beforeEach(() => {
    localStorage.clear();
    // The pre-paint script always runs first in the real app, so both
    // attributes exist before any hook mounts. Reproduce that rather than
    // starting from a bare <html> the app never actually has.
    eval(THEME_BOOTSTRAP_SCRIPT);
  });

  it("defaults to light + full when nothing is stamped", () => {
    const { result } = renderHook(() => useChatTheme());
    expect(result.current.theme).toBe("light");
    expect(result.current.accent).toBe("full");
  });

  it("reads the attributes the pre-paint script stamped", () => {
    document.documentElement.setAttribute(THEME_ATTR, "dark");
    document.documentElement.setAttribute(ACCENT_ATTR, "focus");
    const { result } = renderHook(() => useChatTheme());
    expect(result.current.theme).toBe("dark");
    expect(result.current.accent).toBe("focus");
  });

  it("toggles each axis independently", () => {
    const { result } = renderHook(() => useChatTheme());

    act(() => result.current.toggleTheme());
    expect(attrs()).toEqual({ theme: "dark", accent: "full" });

    act(() => result.current.toggleAccent());
    expect(attrs()).toEqual({ theme: "dark", accent: "focus" });

    // Flipping the theme back must not disturb focus — the whole point of
    // two axes is that neither implies the other.
    act(() => result.current.toggleTheme());
    expect(attrs()).toEqual({ theme: "light", accent: "focus" });
  });

  it("round-trips both axes through localStorage", () => {
    setChatTheme("dark");
    setChatAccent("focus");
    expect(localStorage.getItem(THEME_KEY)).toBe("dark");
    expect(localStorage.getItem(ACCENT_KEY)).toBe("focus");
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

describe("THEME_BOOTSTRAP_SCRIPT", () => {
  beforeEach(() => {
    document.documentElement.removeAttribute(THEME_ATTR);
    document.documentElement.removeAttribute(ACCENT_ATTR);
    localStorage.clear();
  });

  it("stamps the stored preference", () => {
    localStorage.setItem(THEME_KEY, "dark");
    localStorage.setItem(ACCENT_KEY, "focus");
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "dark", accent: "focus" });
  });

  it("stamps the defaults when storage is empty", () => {
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "light", accent: "full" });
  });

  it("coerces unrecognised stored values to the defaults", () => {
    localStorage.setItem(THEME_KEY, "solarized");
    localStorage.setItem(ACCENT_KEY, "");
    eval(THEME_BOOTSTRAP_SCRIPT);
    expect(attrs()).toEqual({ theme: "light", accent: "full" });
  });
});
