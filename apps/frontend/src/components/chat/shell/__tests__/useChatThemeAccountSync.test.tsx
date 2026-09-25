import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { installStoragePolyfill } from "@/test-utils/storagePolyfill";
import { THEME_ATTR, THEME_BOOTSTRAP_SCRIPT, setChatTheme } from "@/hooks/useChatTheme";

const get = vi.fn();
const put = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, put }) }));

import { useChatThemeAccountSync } from "../useChatThemeAccountSync";

const restore = installStoragePolyfill();
afterEach(restore);

beforeEach(() => {
  get.mockReset();
  put.mockReset();
  put.mockResolvedValue({});
  localStorage.clear();
  document.documentElement.removeAttribute(THEME_ATTR);
  // Reproduce the pre-paint script's stamp — the hook reads useChatTheme(),
  // which reads the DOM attribute, and the real app never mounts without it.
  eval(THEME_BOOTSTRAP_SCRIPT);
});

describe("useChatThemeAccountSync", () => {
  it("does nothing while disabled", () => {
    renderHook(() => useChatThemeAccountSync(false));
    expect(get).not.toHaveBeenCalled();
  });

  it("applies the account's theme when it differs from this device's", async () => {
    document.documentElement.setAttribute(THEME_ATTR, "light");
    get.mockResolvedValue({ chatTheme: "dark" });
    renderHook(() => useChatThemeAccountSync(true));
    await waitFor(() => expect(document.documentElement.getAttribute(THEME_ATTR)).toBe("dark"));
  });

  it("leaves the device alone when the account has no stored theme", async () => {
    document.documentElement.setAttribute(THEME_ATTR, "light");
    get.mockResolvedValue({ chatTheme: null });
    renderHook(() => useChatThemeAccountSync(true));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    expect(document.documentElement.getAttribute(THEME_ATTR)).toBe("light");
    expect(put).not.toHaveBeenCalled();
  });

  it("PUTs a later toggle back to the account, best-effort", async () => {
    document.documentElement.setAttribute(THEME_ATTR, "light");
    get.mockResolvedValue({ chatTheme: "light" });
    renderHook(() => useChatThemeAccountSync(true));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));

    // What ThemeToggles' toggleTheme() does under the hood — this hook
    // observes it via useChatTheme's own subscription, not a network call.
    act(() => setChatTheme("dark"));

    await waitFor(() => expect(put).toHaveBeenCalledWith("/users/me/prefs", { chatTheme: "dark" }));
  });

  it("never blocks or throws when the fetch fails", async () => {
    get.mockRejectedValue(new Error("boom"));
    expect(() => renderHook(() => useChatThemeAccountSync(true))).not.toThrow();
    await waitFor(() => expect(get).toHaveBeenCalledTimes(1));
    expect(put).not.toHaveBeenCalled();
  });
});
