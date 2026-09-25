"use client";

import { useEffect, useRef } from "react";
import { useApi } from "@/lib/api";
import { THEME_ATTR, setChatTheme, useChatTheme, type ChatTheme } from "@/hooks/useChatTheme";

type ThemeAccountPrefs = { chatTheme?: ChatTheme | null };

/**
 * Keeps the chat theme in sync with the signed-in human's ACCOUNT, not just
 * this device's localStorage — so a fresh install/webview picks up what they
 * chose elsewhere instead of always landing on the OS/light default.
 * localStorage + the pre-paint script still own the instant, no-flash paint;
 * this only reconciles afterward:
 *
 *  - on mount: fetch the account prefs once. If a theme is set there and
 *    differs from this device's, apply it (updates the DOM + localStorage
 *    via setChatTheme, so it becomes this device's pref too).
 *  - after that fetch lands: PUT any later theme change (a toggle, or the
 *    correction just applied) back to the account, best-effort.
 *
 * Both directions swallow failures — a bad fetch or PUT leaves the device
 * exactly as it was, never blocks or flashes the UI.
 */
export function useChatThemeAccountSync(enabled: boolean): void {
  const api = useApi();
  const { theme } = useChatTheme();
  const readyToPutRef = useRef(false);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => api.get("/users/me/prefs"))
      .then((value) => {
        if (cancelled) return;
        const prefs = value as ThemeAccountPrefs;
        if (prefs.chatTheme && prefs.chatTheme !== document.documentElement.getAttribute(THEME_ATTR)) {
          setChatTheme(prefs.chatTheme);
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) readyToPutRef.current = true;
      });
    return () => {
      cancelled = true;
    };
    // `api` identity churns every render (see useUserSyncOnce); fetch once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  useEffect(() => {
    if (!enabled || !readyToPutRef.current) return;
    Promise.resolve()
      .then(() => api.put("/users/me/prefs", { chatTheme: theme }))
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, enabled]);
}
