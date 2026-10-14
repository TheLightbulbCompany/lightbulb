"use client";

// Desktop navigation shares canonical room state with the Messages dock panel.
import "./workspace.css";

import { useEffect, useState, useSyncExternalStore } from "react";
import { useUser } from "@clerk/nextjs";

import { HealthIndicator } from "@/components/chat/HealthIndicator";
import { useBilling } from "@/hooks/useBilling";
import { MessagingDirectory } from "@/components/messaging/MessagingWorkspace";
import { planLabel, isFreeTierExpired } from "@/lib/planLabel";
import { FreeTierNote } from "@/components/chat/FreeTierNote";
import { TrialRecapCard } from "@/components/messaging/TrialRecapCard";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { DesktopUpdater } from "@/components/desktop/DesktopUpdater";
import { NodeStatusPill } from "@/components/desktop/NodeStatusPill";
import { isDesktopApp } from "@/lib/desktopNotify";
import { activateMessagesPanel } from "./workspaceController";

const noSubscribe = () => () => {};

interface WorkspaceSidebarProps {
  onRecoveryReprovision: () => void;
  /** The dock is available once the runtime is ready or parked. */
  chatReady: boolean;
  /** A destination was picked: the shell closes the phone drawer. */
  onNavigate?: () => void;
}

export function WorkspaceSidebar({ onRecoveryReprovision, onNavigate }: WorkspaceSidebarProps): React.ReactElement {
  const { user } = useUser();
  const { account, isSubscribed } = useBilling();
  // The server has no Tauri bridge, so it renders the browser answer and the client corrects it.
  const desktop = useSyncExternalStore(noSubscribe, isDesktopApp, () => false);

  // Captured once so the footer's expiry check is stable across re-renders.
  const [nowMs] = useState(() => Date.now());
  const subscriptionStatus = account?.subscription_status ?? null;
  const isTrialExpired = isFreeTierExpired(subscriptionStatus, account?.trial_end, nowMs);
  const userName = user?.fullName || user?.firstName || "User";
  // ponytail: first membership stands in for the active org (workspace-first
  // onboarding gives one org per user); read useAuth().orgId if that changes.
  const workspaceName = user?.organizationMemberships?.[0]?.organization?.name || "Lightbulb";
  const userInitials = userName
    .split(" ")
    .map((n: string) => n[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  // Consume the drawer's "freshly installed" marker.
  const { state: drawerState, setFreshlyInstalled } = useMarketplaceDrawer();
  useEffect(() => {
    if (!drawerState.freshlyInstalledAgentId) return;
    const handle = setTimeout(() => setFreshlyInstalled(null), 600);
    return () => clearTimeout(handle);
  }, [drawerState.freshlyInstalledAgentId, setFreshlyInstalled]);

  return (
    <div className="cream-sidebar">
      <div className="sidebar-header">
        <h1 className="workspace-name" title={workspaceName}>{workspaceName}</h1>
      </div>


      <div className="sidebar-scroll">
        {/* Messaging never needs the container, so conversations never wait on it. */}
        <TrialRecapCard />
        <MessagingDirectory onOpenRoom={() => { activateMessagesPanel(); onNavigate?.(); }} />
      </div>


      <div className="sidebar-footer">
        {/* Renders only when the workspace is not healthy. */}
        {(isSubscribed || subscriptionStatus === "trial_local") && (
          <HealthIndicator onRecoveryReprovision={onRecoveryReprovision} />
        )}
        {desktop && <><NodeStatusPill /><DesktopUpdater /></>}
        <div className="user-row">
          <div className="user-avatar">{userInitials}</div>
          <div className="user-info">
            <div className="user-name">{userName}</div>
          </div>
          {planLabel(subscriptionStatus, { expired: isTrialExpired }) && (
            <span className="plan-badge">{planLabel(subscriptionStatus, { expired: isTrialExpired })}</span>
          )}
        </div>
        <FreeTierNote />
      </div>
    </div>
  );
}
