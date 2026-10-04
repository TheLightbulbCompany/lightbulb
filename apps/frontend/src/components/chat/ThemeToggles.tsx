"use client";

import { Moon, Sun } from "lucide-react";
import { useChatTheme } from "@/hooks/useChatTheme";

/**
 * The chat light/dark toggle, on the workspace rail (WorkspaceHeader, bottom
 * slot above Help/Settings/avatar).
 *
 * There used to be a second control here (an "accent"/focus toggle between a
 * cream and a neutral chrome); it was removed 2026-09-25 in favour of always
 * using the neutral look, so this is down to the one theme toggle.
 */
export function ThemeToggles(): React.ReactElement {
  const { theme, toggleTheme } = useChatTheme();
  const nextTheme = theme === "dark" ? "light" : "dark";

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-pressed={theme === "dark"}
      // The label names what the click DOES, not the state it reports —
      // aria-pressed already carries the state, and "Dark mode" on a
      // pressed toggle leaves a screen-reader user guessing which way it
      // goes.
      aria-label={`Switch to ${nextTheme} mode`}
      title={`Switch to ${nextTheme} mode`}
    >
      {theme === "dark"
        ? <Sun size={20} aria-hidden="true" />
        : <Moon size={20} aria-hidden="true" />}
    </button>
  );
}
