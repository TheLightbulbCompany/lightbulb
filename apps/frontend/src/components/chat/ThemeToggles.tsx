"use client";

import { Moon, Sun, Target } from "lucide-react";
import { useChatTheme } from "@/hooks/useChatTheme";

/**
 * The two chat appearance controls, shared verbatim by both shells: the
 * desktop rail (WorkspaceHeader, bottom slot above Help/Settings/avatar) and
 * the mobile sidebar footer (ChatLayout). One component so the two can never
 * drift into offering different options.
 *
 * Both are toggles, not menus — there are exactly two states each, and a menu
 * to pick between two things costs a click and a decision for nothing.
 */
export function ThemeToggles({ className }: { className?: string }): React.ReactElement {
  const { theme, accent, toggleTheme, toggleAccent } = useChatTheme();
  const nextTheme = theme === "dark" ? "light" : "dark";

  return (
    <>
      <button
        type="button"
        className={className}
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
      <button
        type="button"
        className={className}
        onClick={toggleAccent}
        aria-pressed={accent === "focus"}
        aria-label={accent === "focus" ? "Turn off focus mode" : "Turn on focus mode"}
        title={accent === "focus" ? "Turn off focus mode" : "Focus mode"}
      >
        <Target size={20} aria-hidden="true" />
      </button>
    </>
  );
}
