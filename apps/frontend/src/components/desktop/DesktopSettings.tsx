"use client";

import { useSyncExternalStore } from "react";

import { DesktopFileAccess } from "./DesktopFileAccess";
import { DesktopOpenAtLogin } from "./DesktopOpenAtLogin";
import { AgentSessionSwitches, DesktopPermissions, LocalSessions } from "./LocalSessions";

// Settings → Desktop. In a browser: the agent session switches. In the desktop app:
// open at login, agent switches, approved folders, session sharing. Each card renders
// nothing when its own desktop command is missing (an older app build).

const noSubscribe = () => () => {};
const inDesktopApp = () => Boolean((window as Window & { __TAURI__?: { core?: { invoke?: unknown } } }).__TAURI__?.core?.invoke);

export function DesktopSettings() {
  // null on the server and during hydration (no window there), then the real
  // answer: reading window during render would mismatch on hydration.
  const desktop = useSyncExternalStore<boolean | null>(noSubscribe, inDesktopApp, () => null);

  return (
    <div className="settings-panel" id="panel-desktop" role="tabpanel">
      <h1 className="settings-page-title">Desktop</h1>
      {desktop ? (
        <>
          <p className="settings-page-desc">What Lightbulb and your agents can do on this Mac.</p>
          <DesktopOpenAtLogin />
          <DesktopPermissions />
          <DesktopFileAccess />
          <LocalSessions />
        </>
      ) : null}
      {desktop !== null && <AgentSessionSwitches />}
    </div>
  );
}
