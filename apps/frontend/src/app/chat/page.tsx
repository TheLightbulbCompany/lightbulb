"use client";

import { Suspense } from "react";
import dynamic from "next/dynamic";
import { LogoMark } from "@/components/Logo";
import { LiveEventsProvider } from "@/hooks/useLiveEvents";
import { SharedLiveViewHost } from "@/components/workspace/panels/sharedLiveView";
import { HuddleProvider } from "@/components/huddle/HuddleProvider";
import { MarketplaceDrawerProvider } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { RunNotifications } from "@/components/notifications/RunNotifications";
import { SettingsRedirect } from "@/components/workspace/SettingsRedirect";

// Matches WorkspaceShell's own onboarding-gate spinner, which is what it
// renders on first mount anyway (useOnboardingGate starts at "loading"), so
// the chunk fetch reads as the same settle rather than a flash of nothing.
function WorkspaceShellLoading() {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 12, minHeight: "100vh" }}>
      <LogoMark className="brand-mark--hero" />
      <div style={{ color: "var(--midnight-muted)", fontSize: 14 }}>Loading…</div>
    </div>
  );
}

// The one shell, at every width (phones get its rail and sidebar as a drawer).
// The shell is browser-only (it reads window.location and storage), so the server renders
// the loader and the shell mounts on the client.
const WorkspaceShell = dynamic(
  () => import("@/components/workspace/WorkspaceShell").then((m) => m.WorkspaceShell),
  { ssr: false, loading: WorkspaceShellLoading },
);

export default function ChatPage() {
  // The shell reads useSearchParams(); the Suspense boundary keeps the
  // static-export step at build time from erroring on it. The fallback paints
  // the pane colour so any suspension reads as the page settling.
  return (
    <Suspense fallback={<div className="h-dvh w-full" style={{ background: "var(--cream)" }} />}>
      <MarketplaceDrawerProvider>
        <LiveEventsProvider preloadRooms>
          {/* Inside the live provider: a super-agent workspace's old Settings keys open the place in the shell. */}
          <SettingsRedirect>
          {/* Live is held above the shell, so it survives navigation inside /chat. */}
          <HuddleProvider>
            <WorkspaceShell />
          </HuddleProvider>
          <RunNotifications />
          {/* The page's one browser live-view connection, shared by the panel and previews. */}
          <SharedLiveViewHost />
          </SettingsRedirect>
        </LiveEventsProvider>
      </MarketplaceDrawerProvider>
    </Suspense>
  );
}
