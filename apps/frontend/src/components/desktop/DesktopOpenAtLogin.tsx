"use client";

import { useEffect, useState } from "react";

import { SettingRow } from "./SettingRow";
import { notifyApiError } from "@/lib/notify";

// Settings → Desktop → "Open at login". The desktop app turns it on once per
// install (so messages reach you after a restart); from then on this toggle is
// the only thing that changes it. Renders nothing outside the desktop app, or
// in a build older than the autostart plugin.

type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

function getInvoke(): Invoke | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (window as any).__TAURI__?.core?.invoke ?? null;
}

export function DesktopOpenAtLogin() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getInvoke()?.("plugin:autostart|is_enabled")
      .then((value) => setEnabled(value === true))
      .catch(() => setEnabled(null));
  }, []);

  if (enabled === null) return null;

  const toggle = async (next: boolean) => {
    const invoke = getInvoke();
    if (!invoke) return;
    setBusy(true);
    try {
      await invoke(next ? "plugin:autostart|enable" : "plugin:autostart|disable");
      setEnabled(next);
    } catch (err) {
      // Unchanged on failure; the switch keeps showing the real state.
      notifyApiError(err, "Couldn't change Open at login");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-card">
      <SettingRow
        title="Open at login"
        description="Start Lightbulb when you log in, so notifications keep arriving."
        toggle={{ checked: enabled, disabled: busy, onChange: (next) => void toggle(next) }}
      />
    </div>
  );
}
