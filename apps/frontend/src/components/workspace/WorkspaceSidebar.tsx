"use client";

// Desktop navigation shares canonical room state with the conversation (Chat's main view).
import "./workspace.css";

import { useEffect, useState, useSyncExternalStore } from "react";
import { useOrganization, useOrganizationList, useUser } from "@clerk/nextjs";
import { ChevronDown } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

import { HealthIndicator } from "@/components/chat/HealthIndicator";
import { useBilling } from "@/hooks/useBilling";
import { MessagingDirectory } from "@/components/messaging/MessagingWorkspace";
import { planLabel, isFreeTierExpired } from "@/lib/planLabel";
import { FreeTierNote } from "@/components/chat/FreeTierNote";
import { TrialRecapCard } from "@/components/messaging/TrialRecapCard";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { DesktopUpdater } from "@/components/desktop/DesktopUpdater";
import { NodeStatusPill } from "@/components/desktop/NodeStatusPill";
import { NetworkConnect } from "@/components/desktop/NetworkConnect";
import { EngineFault } from "@/components/desktop/EngineFault";
import { isDesktopApp } from "@/lib/desktopNotify";
import { activateMessagesPanel } from "./workspaceController";

const noSubscribe = () => () => {};

interface WorkspaceSidebarProps {
  onRecoveryReprovision: () => void;
  /** The workspace is available once the runtime is ready or parked. */
  chatReady: boolean;
  /** A destination was picked: the shell closes the phone drawer. */
  onNavigate?: () => void;
  id?: string;
  /** Open as the narrow-window drawer: a modal dialog over the conversation. */
  dialog?: boolean;
}

export function WorkspaceSidebar({ onRecoveryReprovision, onNavigate, id, dialog = false }: WorkspaceSidebarProps): React.ReactElement {
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
  // The ACTIVE workspace, never the first membership: Clerk lists memberships newest first, so a
  // person in two workspaces saw the newer one's name over the active one's rooms.
  const { organization } = useOrganization();
  const workspaceName = organization?.name || "Lightbulb";
  // ponytail: first 50 memberships only (Clerk's default page is 10), as on the phone; page when someone belongs to more.
  const { userMemberships, setActive } = useOrganizationList({ userMemberships: { pageSize: 50 } });
  const memberships = userMemberships?.data ?? [];
  const switchWorkspace = async (orgId: string) => {
    if (!setActive || orgId === organization?.id) return;
    await setActive({ organization: orgId });
    // A full load: cached reads, live subscriptions and runtime keys all belong to the old workspace.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- a full load is the point
    window.location.assign("/chat");
  };
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
    <div id={id} className="cream-sidebar" {...(dialog ? { role: "dialog", "aria-modal": true, "aria-label": "Conversations" } : {})}>
      <div className="sidebar-header">
        <h1 className="workspace-name" title={workspaceName}>
          {memberships.length > 1 ? (
            <DropdownMenu>
              <DropdownMenuTrigger className="workspace-switcher">
                {workspaceName}
                <ChevronDown size={14} aria-hidden />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuLabel>Switch workspace</DropdownMenuLabel>
                <DropdownMenuRadioGroup value={organization?.id} onValueChange={(id) => void switchWorkspace(id)}>
                  {memberships.map(({ organization: org }) => (
                    <DropdownMenuRadioItem key={org.id} value={org.id}>{org.name}</DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : workspaceName}
        </h1>
      </div>


      <div className="sidebar-scroll">
        {/* Messaging never needs the container, so conversations never wait on it. */}
        <TrialRecapCard />
        <MessagingDirectory onOpenRoom={() => { activateMessagesPanel(); onNavigate?.(); }} onNavigate={onNavigate} />
      </div>


      <div className="sidebar-footer">
        {/* Renders only when the workspace is broken (see HealthIndicator). */}
        {(isSubscribed || subscriptionStatus === "trial_local") && (
          <HealthIndicator onRecoveryReprovision={onRecoveryReprovision} />
        )}
        {desktop && <><NodeStatusPill /><EngineFault /><NetworkConnect /><DesktopUpdater /></>}
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
