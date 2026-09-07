"use client";

// Desktop workspace chrome — a port of ChatLayout's `.cream-sidebar` block
// (roster + CompanyNav + Gallery) onto the dockview controller. Deltas vs.
// ChatLayout (see task-10 brief):
//   1. Active agent = the dock's focused chat panel (store snapshot), NOT a
//      local userSelectedId — the dock owns focus. Row click → openChatPanel.
//   2. No auto-dispatch effect (the default layout already opens the sentinel
//      chat; a DOM `selectAgent` fan-out here would fight restored layouts).
//   3. No mobile drawer state (sidebarOpen / backdrop / hamburger were mobile
//      chrome; this shell is desktop-only, fixed sidebar).
//   4. SessionList nests under the focused row; onSelect/onNew keep the
//      `useActiveSession` module-store semantics — the focused ChatPanel's
//      transcript follows automatically.
//   5. CompanyNav highlights only when a teams panel is the active dock panel.
//   6/7. Create pill + per-row actions route through openPanel; the agent
//      dialogs are roster-owned and live here.
//   8. On agent delete, close the agent's chat panel via the controller.
import "@/components/chat/ChatLayout.css";
import "./workspace.css";

import { useEffect, useState } from "react";
import { useAuth, useUser } from "@clerk/nextjs";
import { Settings, Plus } from "lucide-react";
import Link from "next/link";

import { capture } from "@/lib/analytics";
import { GallerySection } from "@/components/chat/GallerySection";
import { HealthIndicator } from "@/components/chat/HealthIndicator";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { useAgents, type Agent } from "@/hooks/useAgents";
import { useBilling } from "@/hooks/useBilling";
import { CompanyNav } from "@/components/chat/CompanyNav";
import { Logo } from "@/components/Logo";
import { AgentCreateDialog, AgentRenameDialog, AgentDeleteDialog, SessionDeleteDialog } from "@/components/chat/AgentDialogs";
import { AgentRoster } from "@/components/agents/AgentRoster";
import { useRosterEntries } from "@/components/agents/useRosterEntries";
import { planLabel, isFreeTierExpired } from "@/lib/planLabel";
import { FreeTierNote } from "@/components/chat/FreeTierNote";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { DesktopUpdater } from "@/components/desktop/DesktopUpdater";
import { NodeStatusPill } from "@/components/desktop/NodeStatusPill";
import { emitStep } from "@/components/getstarted/getStartedEvents";
import { TEAMS_PANEL_PREFIX } from "@/components/chat/InlineTeamsPanel";
import { SessionList } from "@/components/chat/SessionList";
import { useActiveSession } from "@/hooks/useActiveSession";
import { useAgentSessions, type AgentSession } from "@/hooks/useAgentSessions";
import { useSellerPublishedAgents } from "@/components/chat/shell/useSellerPublishedAgents";
import { openChatPanel, openPanel, closeChatPanelIfOpen, useWorkspaceState } from "./workspaceController";

interface WorkspaceSidebarProps {
  onRecoveryReprovision: () => void; // wires HealthIndicator → shell's recovery state
  /** Shell-derived "chat surface is live" signal (runtime_phase ready or
   * parked). While false — the whole provisioning window — the create pill
   * is disabled: there's no roster to add an agent to and no Teams
   * workspace to file a task in yet. */
  chatReady: boolean;
}

export function WorkspaceSidebar({ onRecoveryReprovision, chatReady }: WorkspaceSidebarProps): React.ReactElement {
  const { userId: clerkUserId } = useAuth();
  const { user } = useUser();
  const { agents, defaultId, createAgent, deleteAgent, updateAgent } = useAgents();
  const { account, isSubscribed } = useBilling();
  // Models catalog for the sidebar agent rows: resolves Bedrock
  // application-inference-profile ARNs to display names ("Claude Opus 4.6").
  const { data: configSnapshot } = useGatewayRpc<{ config?: unknown }>("config.get");
  // Per-agent runtime state (Teams roster status ∪ live gateway runs). This
  // shell sits inside `TeamsEventsProvider` (see WorkspaceShell), which
  // revalidates `/teams/agents` on `teams.agent.status`, so the rows stay live
  // without polling from here.
  const rosterEntries = useRosterEntries(agents);

  // Delta 1: focus is owned by the dock. The active row / nested SessionList
  // derive from the store snapshot's focused chat panel, not local state.
  const { activePanelId, focusedChatAgentId } = useWorkspaceState();
  const currentAgentId = focusedChatAgentId;

  const [createFormOpen, setCreateFormOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Agent | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Agent | null>(null);
  const [deleteSessionTarget, setDeleteSessionTarget] = useState<AgentSession | null>(null);
  // Captured once (not read directly in render — Date.now() is impure) so
  // the free-tier expiry check below is stable across re-renders. Same
  // pattern as settings/page.tsx's nowMs.
  const [nowMs] = useState(() => Date.now());

  // Per-agent session state. `useActiveSession` is a shared module store, so
  // selecting a row here drives the focused ChatPanel's transcript (same
  // store) — no prop drilling across the dock.
  const { activeSessionId, newSession, selectSession } = useActiveSession(currentAgentId);
  const { sessions: agentSessions, renameSession, deleteSession } = useAgentSessions(
    currentAgentId,
    clerkUserId ?? null,
  );

  const subscriptionStatus = account?.subscription_status ?? null;
  // Keeps the footer chip in sync with TrialBanner/OutOfCreditsBanner
  // elsewhere on this same screen — without this, an expired free-tier
  // owner saw "Free tier" here while the banner already said usage is
  // blocked. See lib/planLabel.ts for why this isn't computed inline.
  const isTrialExpired = isFreeTierExpired(subscriptionStatus, account?.trial_end, nowMs);
  const userName = user?.fullName || user?.firstName || "User";
  const userInitials = userName
    .split(" ")
    .map((n: string) => n[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  // Marketplace drawer: when a hire completes, the provider sets
  // `freshlyInstalledAgentId` so the matching sidebar row plays the `slideIn`
  // animation. Clear the flag after the animation has had time to finish.
  const { state: drawerState, setFreshlyInstalled } = useMarketplaceDrawer();
  useEffect(() => {
    if (!drawerState.freshlyInstalledAgentId) return;
    emitStep("hire");
    const handle = setTimeout(() => setFreshlyInstalled(null), 600);
    return () => clearTimeout(handle);
  }, [drawerState.freshlyInstalledAgentId, setFreshlyInstalled]);

  // Gold pub-dot on the sidebar avatar of agents the user has published.
  const publishedAgentIds = useSellerPublishedAgents();

  function handleSelectAgent(agentId: string): void {
    capture("agent_selected", { agent_id: agentId });
    openChatPanel(agentId);
  }

  // Delta 5: CompanyNav highlights only when a teams panel is the active dock
  // panel; any other active panel (chat, inspector, control) → no highlight.
  const companyActivePanel =
    activePanelId && activePanelId.startsWith(TEAMS_PANEL_PREFIX) ? activePanelId : undefined;

  return (
    <div className="cream-sidebar">
      {/* Header */}
      <div className="sidebar-header">
        <div className="sidebar-logo">
          <Logo size="sm" href={null} />
          <span className="nav-badge">Public Beta</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <Link href="/settings" className="sidebar-settings-link">
            <Settings size={18} />
          </Link>
        </div>
      </div>

      {/* Health Indicator — hidden until the user has a container to report
          on. isSubscribed is the PAID predicate (false for the free tier),
          but trial_local owners have a real container too, so they keep the
          indicator. */}
      {(isSubscribed || subscriptionStatus === "trial_local") && (
        <HealthIndicator onRecoveryReprovision={onRecoveryReprovision} />
      )}

      {/* Node Status */}
      <NodeStatusPill />

      {/* Co-equal create pill: New agent adds to the roster directly; New task
          routes to the Tasks panel and auto-opens its dialog via the one-shot. */}
      <div className="create-pill" role="group" aria-label="Create">
        <button
          type="button"
          className="create-pill__seg"
          onClick={() => setCreateFormOpen(true)}
          aria-label="New agent"
          disabled={!chatReady}
          title={chatReady ? undefined : "Available once your workspace is ready"}
        >
          <Plus size={12} strokeWidth={1.75} />
          Agent
        </button>
        <span className="create-pill__divider" aria-hidden="true" />
        <button
          type="button"
          className="create-pill__seg"
          onClick={() => openPanel("teams-issues", {}, { pushUrl: true, oneShots: { new: "task" } })}
          aria-label="New task"
          disabled={!chatReady}
          title={chatReady ? undefined : "Available once your workspace is ready"}
        >
          <Plus size={12} strokeWidth={1.75} />
          Task
        </button>
      </div>

      {/* Roster + CompanyNav + Gallery share a single `.sidebar-scroll` region
          so the roster keeps its natural height. */}
      <div className="sidebar-scroll">
        <AgentRoster
          entries={rosterEntries}
          activeAgentId={currentAgentId}
          defaultAgentId={defaultId}
          publishedAgentIds={publishedAgentIds}
          freshlyInstalledAgentId={drawerState.freshlyInstalledAgentId}
          configSnapshot={configSnapshot?.config}
          onSelect={handleSelectAgent}
          onOpenWork={(id) => openPanel("teams-issues", { agent: id }, { pushUrl: true })}
          onInspect={(id) => openPanel("agent-inspector", { agent: id }, { pushUrl: true })}
          onRename={setRenameTarget}
          onDelete={setDeleteTarget}
          renderSessions={() => (
            <SessionList
              sessions={agentSessions}
              activeSessionId={activeSessionId}
              onSelect={(sid) => selectSession(sid)}
              onNew={() => newSession()}
              onRename={(s, label) => {
                void renameSession(s.key, label);
              }}
              onDelete={(s) => setDeleteSessionTarget(s)}
            />
          )}
        />
        {/* CompanyNav (Home / Inbox / Work / Knowledge / Company) plus the marketplace
            block (GallerySection). */}
        {/* Same `chatReady` gate as the create pill above. While the stepper
            owns the screen the dock is unmounted, so `openPanel` hits its
            `if (!_api) return` and these clicks did nothing at all — silently.
            Half this sidebar told the truth about that; now all of it does. */}
        <CompanyNav
          activePanel={companyActivePanel}
          onPanelChange={(key) => openPanel(key, {}, { pushUrl: true })}
          disabled={!chatReady}
          disabledTitle="Available once your workspace is ready"
        />
        <GallerySection />
      </div>

      {/* Footer */}
      <div className="sidebar-footer">
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
        <DesktopUpdater version="0.1" />
      </div>

      {/* Roster-owned dialogs. Each is keyed on its open/target so it remounts
          (and re-initializes its internal state) on each open. */}
      <AgentCreateDialog
        key={createFormOpen ? "create-open" : "create-closed"}
        open={createFormOpen}
        existingNames={agents
          .map((a) => a.identity?.name || a.name)
          .filter((n): n is string => !!n)}
        onCancel={() => setCreateFormOpen(false)}
        onCreate={async (name) => {
          await createAgent({ name });
          setCreateFormOpen(false);
        }}
      />

      <AgentRenameDialog
        key={`rename-${renameTarget?.id ?? "closed"}`}
        agent={renameTarget}
        onCancel={() => setRenameTarget(null)}
        onRename={async (name) => {
          if (!renameTarget) return;
          await updateAgent(renameTarget.id, { name });
          setRenameTarget(null);
        }}
      />

      <AgentDeleteDialog
        key={`delete-${deleteTarget?.id ?? "closed"}`}
        agent={deleteTarget}
        onCancel={() => setDeleteTarget(null)}
        onDelete={async () => {
          if (!deleteTarget) return;
          const deletedId = deleteTarget.id;
          await deleteAgent(deletedId);
          // Delta 8: if this agent's chat panel is open in the dock, close it.
          closeChatPanelIfOpen(deletedId);
          setDeleteTarget(null);
        }}
      />

      <SessionDeleteDialog
        key={`delete-session-${deleteSessionTarget?.key ?? "closed"}`}
        session={deleteSessionTarget}
        onCancel={() => setDeleteSessionTarget(null)}
        onDelete={async () => {
          if (!deleteSessionTarget) return;
          const wasActive = deleteSessionTarget.sessionId === activeSessionId;
          await deleteSession(deleteSessionTarget.key);
          // The displayed session no longer exists to stay selected — fall back
          // to General, which always survives (it isn't deletable).
          if (wasActive) selectSession(null);
          setDeleteSessionTarget(null);
        }}
      />
    </div>
  );
}
