import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  LAMP_STORAGE_KEY,
  clearTheming,
  getServerTheme,
  getTheme,
  hydrateFromStorage,
  lightOn,
  setTheme,
  strike,
  subscribe,
  useLampTheme,
} from "../lampStore";

describe("lampStore", () => {
  beforeEach(() => {
    document.documentElement.removeAttribute("data-lb-theme");
    setTheme("light");
    // after setTheme, which persists: every test starts with nothing stored
    window.localStorage.clear();
    clearTheming();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("marks <html> data-lb-theming for 800ms after the LAST strike", () => {
    vi.useFakeTimers();
    const marked = () => document.documentElement.hasAttribute("data-lb-theming");
    strike();
    expect(marked()).toBe(true);
    vi.advanceTimersByTime(500);
    strike(); // inside the window: extends it
    vi.advanceTimersByTime(500); // 1000ms after the first, 500 after the last
    expect(marked()).toBe(true);
    vi.advanceTimersByTime(300);
    expect(marked()).toBe(false);
  });

  it("hydrating a stored theme adopts it without the transition marker", () => {
    window.localStorage.setItem(LAMP_STORAGE_KEY, "dark");
    hydrateFromStorage();
    expect(getTheme()).toBe("dark");
    expect(document.documentElement.hasAttribute("data-lb-theming")).toBe(false);
  });

  it("defaults to light on the server and the client", () => {
    expect(getServerTheme()).toBe("light");
    expect(getTheme()).toBe("light");
  });

  it("strike toggles and writes the contract", () => {
    strike();
    expect(getTheme()).toBe("dark");
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("dark");
    expect(window.localStorage.getItem(LAMP_STORAGE_KEY)).toBe("dark");
    strike();
    expect(getTheme()).toBe("light");
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("light");
  });

  it("lightOn is idempotent and never darkens", () => {
    setTheme("dark");
    lightOn();
    lightOn();
    expect(getTheme()).toBe("light");
  });

  it("hydrateFromStorage adopts a stored dark, ignores junk", () => {
    window.localStorage.setItem(LAMP_STORAGE_KEY, "dark");
    hydrateFromStorage();
    expect(getTheme()).toBe("dark");
    setTheme("light");
    window.localStorage.setItem(LAMP_STORAGE_KEY, "banana");
    hydrateFromStorage();
    expect(getTheme()).toBe("light");
    // hydration is not a write: junk is left where it was
    expect(window.localStorage.getItem(LAMP_STORAGE_KEY)).toBe("banana");
  });

  it("hydrating with nothing stored paints light but stores no choice", () => {
    document.documentElement.removeAttribute("data-lb-theme");
    hydrateFromStorage();
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("light");
    expect(window.localStorage.getItem(LAMP_STORAGE_KEY)).toBeNull();
  });

  it("clearTheming drops the marker and cancels its pending timer", () => {
    vi.useFakeTimers();
    strike();
    expect(document.documentElement.hasAttribute("data-lb-theming")).toBe(true);
    clearTheming();
    expect(document.documentElement.hasAttribute("data-lb-theming")).toBe(false);
    // the cancelled timer must not fire later and cut a fresh marker short
    document.documentElement.setAttribute("data-lb-theming", "");
    vi.advanceTimersByTime(800);
    expect(document.documentElement.hasAttribute("data-lb-theming")).toBe(true);
  });

  it("notifies subscribers once per change", () => {
    let n = 0;
    const off = subscribe(() => { n += 1; });
    strike();
    expect(n).toBe(1);
    off();
    strike();
    expect(n).toBe(1);
  });

  it("useLampTheme tracks the store", () => {
    const { result } = renderHook(() => useLampTheme());
    expect(result.current).toBe("light");
    act(() => strike());
    expect(result.current).toBe("dark");
  });
});
