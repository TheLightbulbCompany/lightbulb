import { beforeEach, describe, expect, it } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  LAMP_STORAGE_KEY,
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
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-lb-theme");
    setTheme("dark");
  });

  it("defaults to dark on the server and the client", () => {
    expect(getServerTheme()).toBe("dark");
    expect(getTheme()).toBe("dark");
  });

  it("strike toggles and writes the contract", () => {
    strike();
    expect(getTheme()).toBe("light");
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("light");
    expect(window.localStorage.getItem(LAMP_STORAGE_KEY)).toBe("light");
    strike();
    expect(getTheme()).toBe("dark");
    expect(document.documentElement.getAttribute("data-lb-theme")).toBe("dark");
  });

  it("lightOn is idempotent and never darkens", () => {
    lightOn();
    lightOn();
    expect(getTheme()).toBe("light");
  });

  it("hydrateFromStorage adopts a stored light, ignores junk", () => {
    window.localStorage.setItem(LAMP_STORAGE_KEY, "light");
    hydrateFromStorage();
    expect(getTheme()).toBe("light");
    window.localStorage.setItem(LAMP_STORAGE_KEY, "banana");
    setTheme("dark");
    hydrateFromStorage();
    expect(getTheme()).toBe("dark");
    expect(window.localStorage.getItem(LAMP_STORAGE_KEY)).toBe("dark");
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
    expect(result.current).toBe("dark");
    act(() => strike());
    expect(result.current).toBe("light");
  });
});
