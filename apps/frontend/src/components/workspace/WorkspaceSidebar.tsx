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

import { useEffect, useMemo, useState } from "react";
import { useAuth, useUser } from "@clerk/nextjs";
import { Settings, Plus, Pencil, Trash2, ListTodo, SlidersHorizontal } from "lucide-react";
import Link from "next/link";

import { capture } from "@/lib/analytics";
import { GallerySection } from "@/components/chat/GallerySection";
import { HealthIndicator } from "@/components/chat/HealthIndicator";
import { useGateway } from "@/hooks/useGateway";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { buildModelNameMap, displayModelName } from "@/lib/modelDisplay";
import { useAgents, getAgentModelString, agentDisplayName, type Agent } from "@/hooks/useAgents";
import { useBilling } from "@/hooks/useBilling";
import { CompanyNav } from "@/components/chat/CompanyNav";
import { Logo } from "@/components/Logo";
import { AgentCreateDialog, AgentRenameDialog, AgentDeleteDialog, SessionDeleteDialog } from "@/components/chat/AgentDialogs";
import { Monogram, StatusDot } from "@/components/agents/AgentIdentity";
import { fromInstalledAgent } from "@/lib/agentDesign";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { DesktopUpdater } from "@/components/desktop/DesktopUpdater";
import { emitStep } from "@/components/getstarted/getStartedEvents";
import { TEAMS_PANEL_PREFIX } from "@/components/chat/InlineTeamsPanel";
import { SessionList } from "@/components/chat/SessionList";
import { useActiveSession } from "@/hooks/useActiveSession";
import { useAgentSessions, type AgentSession } from "@/hooks/useAgentSessions";
import { useSellerPublishedAgents } from "@/components/chat/shell/useSellerPublishedAgents";
import { openChatPanel, openPanel, closeChatPanelIfOpen, useWorkspaceState } from "./workspaceController";

interface WorkspaceSidebarProps {
  onRecoveryReprovision: () => void; // wires HealthIndicator → shell's recovery state
}

export function WorkspaceSidebar({ onRecoveryReprovision }: WorkspaceSidebarProps): React.ReactElement {
  const { userId: clerkUserId } = useAuth();
  const { user } = useUser();
  const { agents, defaultId, createAgent, deleteAgent, updateAgent } = useAgents();
  const { account, isSubscribed } = useBilling();
  const { nodeConnected } = useGateway();
  // Models catalog for the sidebar agent rows: resolves Bedrock
  // application-inference-profile ARNs to display names ("Claude Opus 4.6").
  const { data: configSnapshot } = useGatewayRpc<{ config?: unknown }>("config.get");
  const modelNames = useMemo(() => buildModelNameMap(configSnapshot?.config), [configSnapshot?.config]);

  // Delta 1: focus is owned by the dock. The active row / nested SessionList
  // derive from the store snapshot's focused chat panel, not local state.
  const { activePanelId, focusedChatAgentId } = useWorkspaceState();
  const currentAgentId = focusedChatAgentId;

  const [createFormOpen, setCreateFormOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Agent | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Agent | null>(null);
  const [deleteSessionTarget, setDeleteSessionTarget] = useState<AgentSession | null>(null);

  // Per-agent session state. `useActiveSession` is a shared module store, so
  // selecting a row here drives the focused ChatPanel's transcript (same
  // store) — no prop drilling across the dock.
  const { activeSessionId, newSession, selectSession } = useActiveSession(currentAgentId);
  const { sessions: agentSessions, renameSession, deleteSession } = useAgentSessions(
    currentAgentId,
    clerkUserId ?? null,
  );

  const subscriptionStatus = account?.subscription_status ?? null;
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
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
          <Link href="/settings" className="sidebar-settings-link">
            <Settings size={18} />
          </Link>
        </div>
      </div>

      {/* Health Indicator — hidden until the user has a subscription. */}
      {isSubscribed && <HealthIndicator onRecoveryReprovision={onRecoveryReprovision} />}

      {/* Node Status */}
      {nodeConnected && (
        <div style={{
          padding: '4px 12px',
          fontSize: '12px',
          color: '#16a34a',
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
        }}>
          <span style={{
            width: '6px',
            height: '6px',
            borderRadius: '50%',
            backgroundColor: '#16a34a',
            display: 'inline-block',
          }} />
          Local tools available
        </div>
      )}

      {/* Co-equal create pill: New agent adds to the roster directly; New task
          routes to the Tasks panel and auto-opens its dialog via the one-shot. */}
      <div className="create-pill" role="group" aria-label="Create">
        <button
          type="button"
          className="create-pill__seg"
          onClick={() => setCreateFormOpen(true)}
          aria-label="New agent"
        >
          <Plus size={13} />
          Agent
        </button>
        <span className="create-pill__divider" aria-hidden="true" />
        <button
          type="button"
          className="create-pill__seg"
          onClick={() => openPanel("teams-issues", {}, { pushUrl: true, oneShots: { new: "task" } })}
          aria-label="New task"
        >
          <Plus size={13} />
          Task
        </button>
      </div>

      {/* Roster + CompanyNav + Gallery share a single `.sidebar-scroll` region
          so the roster keeps its natural height. */}
      <div className="sidebar-scroll">
        <div className="agent-list">
          <div className="agent-section-label">
            <span>My Agents</span>
            <b>{agents.length}</b>
          </div>
          {agents.map((agent) => {
            const isDefault = agent.id === defaultId;
            const isNewArrival = drawerState.freshlyInstalledAgentId === agent.id;
            const displayAgent = fromInstalledAgent(agent);
            return (
              <div
                key={agent.id}
                className={`agent-item${currentAgentId === agent.id ? " active" : ""}${isNewArrival ? " new-arrival" : ""}`}
                onClick={() => handleSelectAgent(agent.id)}
              >
                <span className="relative inline-flex">
                  <Monogram agent={displayAgent} size={28} radius={8} />
                  {publishedAgentIds.has(agent.id) && (
                    <span
                      className="absolute top-0 right-0 w-2 h-2 rounded-full bg-[#c2a378] border-2 border-[#faf7f2]"
                      aria-label="Published to marketplace"
                      title="Published to marketplace"
                    />
                  )}
                </span>
                <div className="agent-info">
                  <div className="agent-name">{agentDisplayName(agent)}</div>
                  {(() => {
                    const modelStr = getAgentModelString(agent);
                    const display = displayModelName(modelStr, modelNames);
                    if (!display) return null;
                    return <div className="agent-model">{display}</div>;
                  })()}
                </div>
                <div className="agent-row-actions">
                  {/* "See her work →": deep-link into the Tasks panel filtered
                      to this agent. */}
                  <button
                    type="button"
                    className="agent-row-action"
                    aria-label={`See ${agentDisplayName(agent)}'s work`}
                    title={`See ${agentDisplayName(agent)}'s work`}
                    onClick={(e) => { e.stopPropagation(); openPanel("teams-issues", { agent: agent.id }, { pushUrl: true }); }}
                  >
                    <ListTodo size={13} />
                  </button>
                  {/* "Inspect agent": open the per-agent inspector shell. */}
                  <button
                    type="button"
                    className="agent-row-action"
                    aria-label={`Inspect ${agentDisplayName(agent)}`}
                    title={`Inspect ${agentDisplayName(agent)}`}
                    onClick={(e) => { e.stopPropagation(); openPanel("agent-inspector", { agent: agent.id }, { pushUrl: true }); }}
                  >
                    <SlidersHorizontal size={13} />
                  </button>
                  <button
                    type="button"
                    className="agent-row-action"
                    aria-label={`Rename ${agentDisplayName(agent)}`}
                    onClick={(e) => { e.stopPropagation(); setRenameTarget(agent); }}
                  >
                    <Pencil size={13} />
                  </button>
                  {!isDefault && (
                    <button
                      type="button"
                      className="agent-row-action agent-row-action--danger"
                      aria-label={`Delete ${agentDisplayName(agent)}`}
                      onClick={(e) => { e.stopPropagation(); setDeleteTarget(agent); }}
                    >
                      <Trash2 size={13} />
                    </button>
                  )}
                </div>
                <StatusDot status="active" />
                {currentAgentId === agent.id && (
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
              </div>
            );
          })}
        </div>
        {/* CompanyNav (Dashboard / Inbox / Work / Company) plus the marketplace
            block (GallerySection). */}
        <CompanyNav
          activePanel={companyActivePanel}
          onPanelChange={(key) => openPanel(key, {}, { pushUrl: true })}
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
          {subscriptionStatus && <span className="plan-badge">{subscriptionStatus}</span>}
        </div>
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
