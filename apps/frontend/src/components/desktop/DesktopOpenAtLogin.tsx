"use client";

import { useEffect, useState } from "react";

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
    } catch {
      // Unchanged on failure; the checkbox keeps showing the real state.
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-card">
      <label className="settings-card-header" style={{ cursor: "pointer" }}>
        <div>
          <div className="settings-card-title">Open at login</div>
          <div className="settings-card-desc">
            Start Lightbulb when you log in to your Mac, so notifications keep arriving.
          </div>
        </div>
        <input
          type="checkbox"
          checked={enabled}
          disabled={busy}
          onChange={(event) => void toggle(event.target.checked)}
        />
      </label>
    </div>
  );
}
