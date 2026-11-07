"use client";

import { useSyncExternalStore } from "react";

import { DesktopFileAccess } from "./DesktopFileAccess";
import { DesktopOpenAtLogin } from "./DesktopOpenAtLogin";
import { AgentSessionSwitches, DesktopPermissions, LocalSessions } from "./LocalSessions";

// Settings → Desktop. In a browser: one card with the download. In the desktop app:
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
      ) : desktop === false ? (
        <>
          <p className="settings-page-desc">Pairing, sharing and agent permissions live on your Mac, so open the Lightbulb desktop app to change them.</p>
          <div className="settings-card">
            <div className="settings-card-header">
              <div>
                <div className="settings-card-title">Lightbulb for Mac</div>
                <div className="settings-card-desc">Apple Silicon</div>
              </div>
              {/* Plain <a>: an API route (302 to the signed DMG), not a Next page. */}
              {/* eslint-disable-next-line @next/next/no-html-link-for-pages */}
              <a href="/api/download/desktop" className="settings-btn-outline">Download</a>
            </div>
          </div>
        </>
      ) : null}
      {desktop !== null && <AgentSessionSwitches />}
    </div>
  );
}
