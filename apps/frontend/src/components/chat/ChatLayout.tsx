"use client";

import "./ChatLayout.css";
import { useEffect, useRef, useState } from "react";
import { useAuth, useUser, UserButton } from "@clerk/nextjs";

import { capture } from "@/lib/analytics";
import { useSearchParams } from "next/navigation";
import { Settings, Plus, CheckCircle, CreditCard, Menu, X, FolderOpen, HelpCircle, ListTodo, SlidersHorizontal, Pause, Play } from "lucide-react";
import Link from "next/link";

import { ProvisioningStepper } from "@/components/chat/ProvisioningStepper";
import { GallerySection } from "@/components/chat/GallerySection";
import { HealthIndicator } from "@/components/chat/HealthIndicator";
import { TrialBanner } from "@/components/chat/TrialBanner";
import { OutOfCreditsBanner } from "@/components/chat/OutOfCreditsBanner";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { useAgents, type Agent } from "@/hooks/useAgents";
import { useBilling } from "@/hooks/useBilling";
import { useOnboardingGate } from "@/hooks/useOnboardingGate";
import { CompanyNav } from "@/components/chat/CompanyNav";
import { FileViewer } from "@/components/chat/FileViewer";
import { Logo } from "@/components/Logo";
import { useSupport } from "@/components/support/SupportProvider";
import { AgentCreateDialog, AgentRenameDialog, AgentDeleteDialog, SessionDeleteDialog } from "@/components/chat/AgentDialogs";
import { AgentRoster } from "@/components/agents/AgentRoster";
import { useRosterEntries } from "@/components/agents/useRosterEntries";
import { planLabel, isFreeTierExpired } from "@/lib/planLabel";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { NodeStatusPill } from "@/components/desktop/NodeStatusPill";
import { DesktopUpdater } from "@/components/desktop/DesktopUpdater";
import { DesktopPermissionsGate } from "@/components/desktop/DesktopPermissionsGate";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { emitStep } from "@/components/getstarted/getStartedEvents";
import { GetStarted } from "@/components/getstarted/GetStarted";
import { TEAMS_PANEL_PREFIX } from "@/components/chat/InlineTeamsPanel";
import { pushSearchParams } from "@/lib/shellNav";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { SessionList } from "@/components/chat/SessionList";
import { useActiveSession } from "@/hooks/useActiveSession";
import { useAgentSessions, type AgentSession } from "@/hooks/useAgentSessions";
import { useUserSyncOnce } from "@/components/chat/shell/useUserSyncOnce";
import { useCheckoutReturn } from "@/components/chat/shell/useCheckoutReturn";
import { useSellerPublishedAgents } from "@/components/chat/shell/useSellerPublishedAgents";
import { useGetStartedDerived } from "@/components/chat/shell/useGetStartedDerived";

interface ChatLayoutProps {
  children: React.ReactNode;
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
  openAgentIssues?: (agentId: string) => void;
  openAgentInspector?: (agentId: string) => void;
  onClearPanel?: () => void;
  onNewTask?: () => void;
  fileViewerOpen?: boolean;
  activeFilePath?: string | null;
  onOpenFile?: (path: string) => void;
  onCloseFileViewer?: () => void;
}

function dispatchSelectAgentEvent(agentId: string): void {
  window.dispatchEvent(
    new CustomEvent("selectAgent", { detail: { agentId } }),
  );
}

export function ChatLayout({
  children,
  activePanel,
  onPanelChange,
  openAgentIssues,
  openAgentInspector,
  onClearPanel,
  onNewTask,
  fileViewerOpen,
  activeFilePath,
  onOpenFile,
  onCloseFileViewer,
}: ChatLayoutProps): React.ReactElement {
  // Block the whole chat shell until Clerk hydration + onboarding state are
  // settled AND any pending org auto-activation has landed. This is the
  // hinge for the personal/org race fix: if we render ProvisioningStepper
  // (or any owner_id-aware hook) before the JWT has finished flipping into
  // its final context, it fires POST /container/provision and /billing
  // reads with a stale JWT and we end up with orphan personal rows for a
  // user who was always meant to be an org member. The state machine +
  // the router.replace / setActive side effects live in the hook.
  const onboardingPhase = useOnboardingGate();
  const { isSignedIn, userId: clerkUserId } = useAuth();
  const { user } = useUser();
  const { agents, defaultId, createAgent, deleteAgent, updateAgent } = useAgents();
  const { account, isSubscribed } = useBilling();
  // Models catalog for the sidebar agent rows: resolves Bedrock
  // application-inference-profile ARNs to display names ("Claude Opus 4.6")
  // — the raw ARN's last segment is an opaque random suffix.
  const { data: configSnapshot } = useGatewayRpc<{ config?: unknown }>("config.get");
  // Per-agent runtime state for the roster (Teams roster status ∪ live gateway
  // runs). Shares the `/teams/agents` SWR key the pause/resume control below
  // already reads, so this adds no request on this shell.
  const rosterEntries = useRosterEntries(agents, defaultId);
  const searchParams = useSearchParams();

  const [userSelectedId, setUserSelectedId] = useState<string | null>(null);
  // Stripe checkout-return handling (banner + webhook polling + URL cleanup)
  // lives in the shared shell hook.
  const { showSubscriptionSuccess } = useCheckoutReturn();
  const [recoveryTriggered, setRecoveryTriggered] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { openSupport } = useSupport();
  const [createFormOpen, setCreateFormOpen] = useState(false);
  const [renameTarget, setRenameTarget] = useState<Agent | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Agent | null>(null);
  const [deleteSessionTarget, setDeleteSessionTarget] = useState<AgentSession | null>(null);
  // Captured once (not read directly in render — Date.now() is impure) so
  // the free-tier expiry check below is stable across re-renders. Same
  // pattern as settings/page.tsx's nowMs.
  const [nowMs] = useState(() => Date.now());

  // Derive effective agent: user selection > default > first agent
  const currentAgentId = userSelectedId ?? defaultId ?? agents[0]?.id ?? null;

  // Per-agent session state. `useActiveSession` is a shared module store, so
  // selecting a row here drives the transcript in AgentChatWindow's
  // useAgentChat (same store) — no prop drilling across subtrees.
  const { activeSessionId, newSession, selectSession } = useActiveSession(currentAgentId);
  const { sessions: agentSessions, renameSession, deleteSession } = useAgentSessions(
    currentAgentId,
    clerkUserId ?? null,
  );

  // Header agent-action cluster (next to the file-tree button): pause/resume +
  // inspector/work deep links for the active agent, so the user can act on the
  // agent they're chatting with without opening the full inspector.
  //
  // The gateway `agents.list` (above) carries no run state, so the paused flag
  // comes from the Teams BFF roster (`/teams/agents`), keyed by the same agent
  // UUID. `useAgentMutations.pause/resume` revalidate that same `/teams/agents`
  // key, so toggling here reflects back into the icon without a manual refetch.
  // `useTeamsApi` is bearer-only (no Teams provider context) so it's safe to
  // call from the chat shell. Best-effort — a roster fetch failure just leaves
  // the agent un-paused-looking; the toggle still works.
  const { read: readTeams } = useTeamsApi();
  const { data: teamsAgentsData } = readTeams<{ agents?: { id: string; status?: string | null }[] }>(
    "/agents",
  );
  const { pause: pauseAgent, resume: resumeAgent } = useAgentMutations();
  const currentTeamsAgent = teamsAgentsData?.agents?.find((a) => a.id === currentAgentId) ?? null;
  const currentAgentPaused = currentTeamsAgent?.status === "paused";
  const [agentActionBusy, setAgentActionBusy] = useState(false);

  async function handleTogglePauseResume(): Promise<void> {
    if (!currentAgentId || agentActionBusy) return;
    setAgentActionBusy(true);
    try {
      if (currentAgentPaused) {
        await resumeAgent(currentAgentId);
      } else {
        await pauseAgent(currentAgentId);
      }
    } finally {
      setAgentActionBusy(false);
    }
  }

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

  // One-shot /users/sync (see the hook for the JWT-gating + latch rationale).
  // Enabled = the same guard set the original effect's early returns used.
  useUserSyncOnce(Boolean(isSignedIn) && onboardingPhase === "ready");

  // Dispatch DOM event so page.tsx picks up the current agent (external system sync)
  const lastDispatchedRef = useRef<string | null>(null);
  useEffect(() => {
    if (currentAgentId && currentAgentId !== lastDispatchedRef.current) {
      lastDispatchedRef.current = currentAgentId;
      dispatchSelectAgentEvent(currentAgentId);
    }
  }, [currentAgentId]);

  // Marketplace drawer: when a hire completes, the provider sets
  // `freshlyInstalledAgentId` so the matching sidebar row plays the
  // `slideIn` animation. Clear the flag after the animation has had time
  // to finish so the class isn't reapplied on subsequent renders.
  const { state: drawerState, setFreshlyInstalled, open: openMarketplace } = useMarketplaceDrawer();

  useEffect(() => {
    if (!drawerState.freshlyInstalledAgentId) return;
    emitStep("hire");
    const handle = setTimeout(() => setFreshlyInstalled(null), 600);
    return () => clearTimeout(handle);
  }, [drawerState.freshlyInstalledAgentId, setFreshlyInstalled]);

  // Gold pub-dot on the sidebar avatar of agents the user has published —
  // the seller-listings read + cross-reference lives in the shared shell hook.
  const publishedAgentIds = useSellerPublishedAgents();

  // "Get started" guide. `chatReady` (the signal that the chat surface is
  // actually live and interactive) and the four completion signals
  // (chat/hire/channel/task) are all derived in the shared shell hook — see
  // its doc comment for the full readiness-gate rationale (why `chatReady`
  // is stronger than `useProvisioningState().phase === "normal"`) and why
  // each completion read stays null-keyed until `chatReady`.
  const { chatReady, ...getStartedDerived } = useGetStartedDerived();

  function handleSelectAgent(agentId: string): void {
    capture("agent_selected", { agent_id: agentId });
    setUserSelectedId(agentId);
    dispatchSelectAgentEvent(agentId);
    // There's no tab-switcher: the view is derived from ?panel. Selecting an
    // agent must drop ?panel so the content area falls back to chat (with the
    // just-selected agent).
    onClearPanel?.();
    setSidebarOpen(false);
  }

  // Toast click-through: RunNotifications dispatches selectAgent with
  // source:"notification". Route it through handleSelectAgent so selection,
  // ?panel cleanup, and the sidebar all behave exactly like a sidebar click.
  // ChatLayout's own dispatches carry no source, so this cannot loop.
  const handleSelectAgentRef = useRef(handleSelectAgent);
  useEffect(() => {
    handleSelectAgentRef.current = handleSelectAgent;
  });
  useEffect(() => {
    function onExternalSelectAgent(e: Event) {
      const detail = (e as CustomEvent<{ agentId?: string; source?: string }>).detail;
      if (detail?.source !== "notification" || !detail.agentId) return;
      handleSelectAgentRef.current(detail.agentId);
    }
    window.addEventListener("selectAgent", onExternalSelectAgent);
    return () => window.removeEventListener("selectAgent", onExternalSelectAgent);
  }, []);

  async function handleCreateAgent(): Promise<void> {
    setCreateFormOpen(true);
  }

  // Block the chat shell until the gate is ``ready``. See
  // ``useOnboardingGate`` for the full state-machine; the rule is the
  // same as before — anything other than ``ready`` means we're either
  // still loading, mid-redirect to /onboarding, or auto-activating an org
  // membership, none of which should let owner_id-aware hooks render.
  if (onboardingPhase !== "ready") {
    return (
      <div className="app-shell" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
        <div style={{ color: "#6b6b6b", fontSize: 14 }}>Loading…</div>
      </div>
    );
  }

  return (
    <>
      <DesktopPermissionsGate />
      <div className={`app-shell${fileViewerOpen ? " with-file-viewer" : ""}`}>
        <div className={`sidebar-backdrop${sidebarOpen ? " visible" : ""}`} onClick={() => setSidebarOpen(false)} />
        <div className={`cream-sidebar${sidebarOpen ? " mobile-open" : ""}`}>
          {/* Header */}
          <div className="sidebar-header">
            <div className="sidebar-logo">
              <Logo size="sm" href={null} />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <Link href="/settings" className="sidebar-settings-link">
                <Settings size={18} />
              </Link>
              <button className="mobile-hamburger" onClick={() => setSidebarOpen(false)} aria-label="Close menu" style={{ display: sidebarOpen ? "flex" : undefined }}>
                <X size={18} />
              </button>
            </div>
          </div>

          {/* Health Indicator — hidden until the user has a container to
              report on; for a pre-signup user "Starting..." is misleading.
              isSubscribed is the PAID predicate (false for the free tier),
              but trial_local owners have a real container too, so they keep
              the indicator. */}
          {(isSubscribed || subscriptionStatus === "trial_local") && (
            <HealthIndicator onRecoveryReprovision={() => setRecoveryTriggered(true)} />
          )}

          {/* Node Status */}
          <NodeStatusPill />

          {/* ONE sidebar body regardless of view: the roster + CompanyNav cover
              both chat and control. The view is derived from ?panel (selecting
              an agent shows chat; a Company/System panel shows that panel), so
              there's no Chat|Control tab-switcher. */}
          {/* Co-equal create pill: the two central creation actions — New
              agent and New task — as equal halves of one forest control.
              New agent adds to the roster directly; New task routes to the
              Tasks panel and auto-opens its dialog (chat/page → onNewTask). */}
          <div className="create-pill" role="group" aria-label="Create">
            <button
              type="button"
              className="create-pill__seg"
              onClick={handleCreateAgent}
              aria-label="New agent"
              disabled={!chatReady}
              title={chatReady ? undefined : "Available once your workspace is ready"}
            >
              <Plus size={13} />
              Agent
            </button>
            <span className="create-pill__divider" aria-hidden="true" />
            <button
              type="button"
              className="create-pill__seg"
              onClick={() => onNewTask?.()}
              aria-label="New task"
              disabled={!chatReady}
              title={chatReady ? undefined : "Available once your workspace is ready"}
            >
              <Plus size={13} />
              Task
            </button>
          </div>

              {/* Agent List + CompanyNav + Gallery share a single
                  `.sidebar-scroll` region so the roster keeps its natural
                  height instead of collapsing under CompanyNav's own scroll. */}
              <div className="sidebar-scroll">
              <AgentRoster
                entries={rosterEntries}
                activeAgentId={currentAgentId}
                defaultAgentId={defaultId}
                publishedAgentIds={publishedAgentIds}
                freshlyInstalledAgentId={drawerState.freshlyInstalledAgentId}
                configSnapshot={configSnapshot?.config}
                onSelect={handleSelectAgent}
                onOpenWork={(id) => openAgentIssues?.(id)}
                onInspect={(id) => openAgentInspector?.(id)}
                onRename={setRenameTarget}
                onDelete={setDeleteTarget}
                renderSessions={() => (
                  <SessionList
                    sessions={agentSessions}
                    activeSessionId={activeSessionId}
                    onSelect={(sid) => {
                      selectSession(sid);
                      // Like handleSelectAgent: the view is derived from
                      // ?panel, so picking a session from a panel view
                      // must drop ?panel to land back on the chat.
                      onClearPanel?.();
                      setSidebarOpen(false);
                    }}
                    onNew={() => {
                      newSession();
                      onClearPanel?.();
                      setSidebarOpen(false);
                    }}
                    onRename={(s, label) => {
                      void renameSession(s.key, label);
                    }}
                    onDelete={(s) => setDeleteSessionTarget(s)}
                  />
                )}
              />
              {/* CompanyNav (Dashboard / Inbox / Work / Company) plus the
                  marketplace block (GallerySection) — Browse + Publish buttons
                  that open the MarketplaceDrawer. The drawer (mounted on the
                  chat page) owns the install + auto-select flow now. */}
              <CompanyNav activePanel={activePanel} onPanelChange={onPanelChange} />
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
            <DesktopUpdater version="0.1" />
          </div>
        </div>

        <div className="main-area" style={{ gridArea: "main" }}>
          <div className="main-header">
            <button className="mobile-hamburger" onClick={() => setSidebarOpen(true)} aria-label="Open menu">
              <Menu size={22} />
            </button>
            {onOpenFile && (
              <button
                onClick={() => { capture("file_browser_opened"); onOpenFile?.(""); }}
                className="flex items-center justify-center text-[var(--mute)] hover:text-[var(--ink)] transition-colors p-1"
                title="Browse workspace files"
              >
                <FolderOpen size={18} />
              </button>
            )}
            {/* Active-agent quick actions — inspector / work / pause-resume —
                so the user can act on the agent they're chatting with without
                opening the full inspector. Gated on an active agent (matches
                the file-tree button's simple presence gate). Destructive /
                structural actions (Delete, Reports-to) stay in the inspector. */}
            {currentAgentId && (
              <div className="flex items-center gap-1" aria-label="Agent actions">
                <button
                  type="button"
                  onClick={() => openAgentInspector?.(currentAgentId)}
                  className="flex items-center justify-center text-[var(--mute)] hover:text-[var(--ink)] transition-colors p-1 rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)]"
                  title="Open agent inspector"
                  aria-label="Open agent inspector"
                >
                  <SlidersHorizontal size={18} />
                </button>
                <button
                  type="button"
                  onClick={() => openAgentIssues?.(currentAgentId)}
                  className="flex items-center justify-center text-[var(--mute)] hover:text-[var(--ink)] transition-colors p-1 rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)]"
                  title="See this agent's work"
                  aria-label="See this agent's work"
                >
                  <ListTodo size={18} />
                </button>
                <button
                  type="button"
                  onClick={() => { void handleTogglePauseResume(); }}
                  disabled={agentActionBusy}
                  className="flex items-center justify-center text-[var(--mute)] hover:text-[var(--ink)] transition-colors p-1 rounded focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)] disabled:opacity-50"
                  title={currentAgentPaused ? "Resume agent" : "Pause agent"}
                  aria-label={currentAgentPaused ? "Resume agent" : "Pause agent"}
                >
                  {currentAgentPaused ? <Play size={18} /> : <Pause size={18} />}
                </button>
              </div>
            )}
            <div style={{ flex: 1 }} />
            <button
              onClick={() => openSupport()}
              className="flex items-center justify-center text-[var(--mute)] hover:text-[var(--ink)] transition-colors p-1 mr-1"
              title="Get help / report a problem"
              aria-label="Get help"
            >
              <HelpCircle size={18} />
            </button>
            <UserButton
              appearance={{
                elements: {
                  avatarBox: "h-8 w-8",
                },
              }}
            >
              <UserButton.MenuItems>
                <UserButton.Link label="Settings" labelIcon={<CreditCard className="h-4 w-4" />} href="/settings" />
              </UserButton.MenuItems>
            </UserButton>
          </div>

          <div className="main-content">
            <TrialBanner />
            <OutOfCreditsBanner />
            {showSubscriptionSuccess && (
              <div className="subscription-banner">
                <CheckCircle size={16} />
                <p>Subscription confirmed! Your agent is being upgraded.</p>
              </div>
            )}
            {recoveryTriggered ? (
              <ProvisioningStepper trigger="recovery">{children}</ProvisioningStepper>
            ) : (
              <ProvisioningStepper>{children}</ProvisioningStepper>
            )}
          </div>

          {chatReady && (
            <GetStarted
              derived={getStartedDerived}
              actions={{
                chat: () => {
                  // Don't select/act on an empty id: if no real agent exists
                  // yet, bail rather than passing "" to handleSelectAgent.
                  const targetId = defaultId ?? agents[0]?.id;
                  if (!targetId) return;
                  handleSelectAgent(targetId);
                  window.dispatchEvent(new CustomEvent("getstarted:focus-composer"));
                },
                task: () => {
                  // ONE navigation to the inbox panel with the New-task
                  // intent. Mirror what `setActivePanel` (page.tsx) would
                  // build — set panel, drop stale agent/detailId filters —
                  // and add `newIssue=onboarding` so the inbox auto-opens its
                  // New-task dialog (read by InboxPage) pre-filled with the
                  // CEO-directed first task. Calling `onPanelChange` and then
                  // a second relative `?panel=…` push grew two history
                  // entries (Back needed two presses) and the relative form
                  // dropped the other query params setActivePanel preserves.
                  const params = new URLSearchParams(searchParams.toString());
                  params.set("panel", `${TEAMS_PANEL_PREFIX}inbox`);
                  params.delete("agent");
                  params.delete("detailId");
                  params.set("newIssue", "onboarding");
                  pushSearchParams(params);
                },
                hire: () => openMarketplace({ view: "browse" }),
                channel: () => {
                  // Route to a surface where a fresh user can actually CREATE
                  // their first bot: the default agent's inspector Channels tab
                  // (AgentChannelsSection). The old /settings?panel=channels
                  // (MyChannelsSection) is link-only and dead-ends a zero-bot
                  // user. ?inspectorTab=channels is read by AgentInspectorPanel
                  // to open straight on that tab.
                  // Bail without navigating if there's no real agent to target
                  // rather than pushing ?agent= with an empty id.
                  const agentId = defaultId ?? agents[0]?.id;
                  if (!agentId) return;
                  const params = new URLSearchParams(searchParams.toString());
                  params.set("panel", "agent-inspector");
                  params.set("agent", agentId);
                  params.set("inspectorTab", "channels");
                  pushSearchParams(params);
                },
              }}
            />
          )}
        </div>

        {fileViewerOpen && (
          <FileViewer
            agentId={currentAgentId}
            initialFilePath={activeFilePath}
            onClose={() => onCloseFileViewer?.()}
          />
        )}

        {/* Each dialog is keyed on its open/target so it remounts (and
            re-initializes its internal state) on each open, instead of
            using useEffect to reset on close — see AgentDialogs.tsx. */}
        <AgentCreateDialog
          key={createFormOpen ? "create-open" : "create-closed"}
          open={createFormOpen}
          // Names only — the dup check is a name-space concern. The UUID id
          // is the identity (used for workspace/RPC targeting), never a name,
          // so an unnamed agent contributes nothing to collide against.
          existingNames={agents
            .map((a) => a.identity?.name || a.name)
            .filter((n): n is string => !!n)}
          onCancel={() => setCreateFormOpen(false)}
          onCreate={async (name) => {
            // OpenClaw's agents.create requires a non-empty `workspace` —
            // see useAgents.createAgent, which fills in the default path
            // (.openclaw/workspace-{id}) when omitted.
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
            await deleteAgent(deleteTarget.id);
            if (userSelectedId === deleteTarget.id) setUserSelectedId(null);
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
            // The displayed session no longer exists to stay selected — fall
            // back to General, which always survives (it isn't deletable).
            if (wasActive) selectSession(null);
            setDeleteSessionTarget(null);
          }}
        />
      </div>
    </>
  );
}
