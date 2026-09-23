"use client";

import "./ChatLayout.css";
import { useEffect, useRef, useState } from "react";
import { OrganizationList, useAuth, useUser, UserButton } from "@clerk/nextjs";

import { capture } from "@/lib/analytics";
import { Settings, Plus, CheckCircle, CreditCard, Menu, X, FolderOpen, HelpCircle, ListTodo, SlidersHorizontal, Pause, Play } from "lucide-react";
import Link from "next/link";

import { ProvisioningStepper } from "@/components/chat/ProvisioningStepper";
import { GallerySection } from "@/components/chat/GallerySection";
import { HealthIndicator } from "@/components/chat/HealthIndicator";
import { TrialBanner } from "@/components/chat/TrialBanner";
import { OutOfCreditsBanner } from "@/components/chat/OutOfCreditsBanner";
import { LaunchJoinBanner } from "@/components/chat/LaunchJoinBanner";
import { LaunchJoinModal } from "@/components/chat/LaunchJoinModal";
import { useAgents, type Agent } from "@/hooks/useAgents";
import { useBilling } from "@/hooks/useBilling";
import { useOnboardingGate } from "@/hooks/useOnboardingGate";
import { CompanyNav } from "@/components/chat/CompanyNav";
import { FileViewer } from "@/components/chat/FileViewer";
import { Logo } from "@/components/Logo";
import { useSupport } from "@/components/support/SupportProvider";
import { AgentCreateDialog, AgentRenameDialog, AgentDeleteDialog } from "@/components/chat/AgentDialogs";
import { AgentRoster } from "@/components/agents/AgentRoster";
import { useRosterEntries } from "@/components/agents/useRosterEntries";
import { planLabel, isFreeTierExpired } from "@/lib/planLabel";
import { FreeTierNote } from "@/components/chat/FreeTierNote";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { NodeStatusPill } from "@/components/desktop/NodeStatusPill";
import { DesktopUpdater } from "@/components/desktop/DesktopUpdater";
import { DesktopPermissionsGate } from "@/components/desktop/DesktopPermissionsGate";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { emitStep } from "@/components/getstarted/getStartedEvents";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { useUserSyncOnce } from "@/components/chat/shell/useUserSyncOnce";
import { useCheckoutReturn } from "@/components/chat/shell/useCheckoutReturn";
import { useSellerPublishedAgents } from "@/components/chat/shell/useSellerPublishedAgents";
import { useChatReady } from "@/components/chat/shell/useChatReady";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ThemeToggles } from "@/components/chat/ThemeToggles";

interface ChatLayoutProps {
  children: React.ReactNode;
  statusIndicator?: React.ReactNode;
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

function dispatchSelectAgentEvent(agentId: string, source?: "default"): void {
  window.dispatchEvent(
    new CustomEvent("selectAgent", { detail: { agentId, source } }),
  );
}

export function ChatLayout({
  children,
  statusIndicator,
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
  const { isSignedIn } = useAuth();
  const { user } = useUser();
  const { agents, defaultId, createAgent, deleteAgent, updateAgent } = useAgents();
  const { account, isSubscribed } = useBilling();
  // Per-agent runtime state for the roster (Teams roster status ∪ live gateway
  // runs). Shares the `/teams/agents` SWR key the pause/resume control below
  // already reads, so this adds no request on this shell.
  const rosterEntries = useRosterEntries(agents);

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
  // Captured once (not read directly in render — Date.now() is impure) so
  // the free-tier expiry check below is stable across re-renders. Same
  // pattern as settings/page.tsx's nowMs.
  const [nowMs] = useState(() => Date.now());

  // Derive effective agent: user selection > default > first agent
  const currentAgentId = userSelectedId ?? defaultId ?? agents[0]?.id ?? null;

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
      // A derived fallback is not a pick: page.tsx leaves it to the provider,
      // which lands a fresh workspace in #general instead.
      dispatchSelectAgentEvent(currentAgentId, userSelectedId ? undefined : "default");
    }
  }, [currentAgentId, userSelectedId]);

  // Marketplace drawer: when a hire completes, the provider sets
  // `freshlyInstalledAgentId` so the matching sidebar row plays the
  // `slideIn` animation. Clear the flag after the animation has had time
  // to finish so the class isn't reapplied on subsequent renders.
  const { state: drawerState, setFreshlyInstalled } = useMarketplaceDrawer();

  useEffect(() => {
    if (!drawerState.freshlyInstalledAgentId) return;
    emitStep("hire");
    const handle = setTimeout(() => setFreshlyInstalled(null), 600);
    return () => clearTimeout(handle);
  }, [drawerState.freshlyInstalledAgentId, setFreshlyInstalled]);

  // Gold pub-dot on the sidebar avatar of agents the user has published —
  // the seller-listings read + cross-reference lives in the shared shell hook.
  const publishedAgentIds = useSellerPublishedAgents();

  const { chatReady } = useChatReady();

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
  const onClearPanelRef = useRef(onClearPanel);
  useEffect(() => {
    handleSelectAgentRef.current = handleSelectAgent;
    onClearPanelRef.current = onClearPanel;
  });
  useEffect(() => {
    function onExternalSelectAgent(e: Event) {
      const detail = (e as CustomEvent<{ agentId?: string; source?: string }>).detail;
      if (detail?.source !== "notification" || !detail.agentId) return;
      handleSelectAgentRef.current(detail.agentId);
    }
    // A run toast's View opens the run's room (MessagingWorkspaceContext selects
    // it); on this shell that room only shows once ?panel is dropped back to chat.
    function onOpenRoom() {
      onClearPanelRef.current?.();
      setSidebarOpen(false);
    }
    window.addEventListener("selectAgent", onExternalSelectAgent);
    window.addEventListener("openRoom", onOpenRoom);
    return () => {
      window.removeEventListener("selectAgent", onExternalSelectAgent);
      window.removeEventListener("openRoom", onOpenRoom);
    };
  }, []);

  async function handleCreateAgent(): Promise<void> {
    setCreateFormOpen(true);
  }

  if (onboardingPhase === "selecting") {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-6 p-4">
        <h1 className="text-2xl font-bold">Choose your workspace</h1>
        <OrganizationList hidePersonal afterSelectOrganizationUrl="/chat" afterCreateOrganizationUrl="/onboarding" skipInvitationScreen />
      </main>
    );
  }
  // The shell waits for routing; useProvisioningState independently gates polling.
  if (onboardingPhase !== "ready") {
    return (
      <div className="app-shell" style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
        <div style={{ color: "var(--midnight-muted)", fontSize: 14 }}>Loading…</div>
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
              <span className="nav-badge">Public Beta</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
              <Link href="/settings" className="sidebar-settings-link">
                <Settings size={16} />
              </Link>
              <button className="mobile-hamburger" onClick={() => setSidebarOpen(false)} aria-label="Close menu" style={{ display: sidebarOpen ? "flex" : undefined }}>
                <X size={16} />
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
              <Plus size={12} />
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
              <Plus size={12} />
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
                onSelect={handleSelectAgent}
                onOpenWork={(id) => openAgentIssues?.(id)}
                onInspect={(id) => openAgentInspector?.(id)}
                onRename={setRenameTarget}
                onDelete={setDeleteTarget}
              />
              {/* CompanyNav (Home / Inbox / Work / Knowledge / Company) plus the
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
            <FreeTierNote />
            {/* Mobile has no rail, so the appearance controls live here — the
                nearest equivalent bottom-left slot to the desktop rail's. */}
            <div className="appearance-row" role="group" aria-label="Appearance">
              <ThemeToggles className="appearance-toggle" />
            </div>
            <DesktopUpdater version="0.1" />
          </div>
        </div>

        <div className="main-area" style={{ gridArea: "main" }}>
          <div className="main-header">
            <button className="mobile-hamburger" onClick={() => setSidebarOpen(true)} aria-label="Open menu">
              <Menu size={20} />
            </button>
            {onOpenFile && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    onClick={() => { capture("file_browser_opened"); onOpenFile?.(""); }}
                    aria-label="Browse workspace files"
                    className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1"
                  >
                    <FolderOpen size={16} />
                  </button>
                </TooltipTrigger>
                <TooltipContent>Browse workspace files</TooltipContent>
              </Tooltip>
            )}
            {/* Active-agent quick actions — inspector / work / pause-resume —
                so the user can act on the agent they're chatting with without
                opening the full inspector. Gated on an active agent (matches
                the file-tree button's simple presence gate). Destructive /
                structural actions (Delete, Reports-to) stay in the inspector. */}
            {currentAgentId && (
              <div className="flex items-center gap-1" aria-label="Agent actions">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => openAgentInspector?.(currentAgentId)}
                      className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1 rounded-sm"
                      aria-label="Open agent inspector"
                    >
                      <SlidersHorizontal size={16} />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>Open agent inspector</TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => openAgentIssues?.(currentAgentId)}
                      className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1 rounded-sm"
                      aria-label="See this agent's work"
                    >
                      <ListTodo size={16} />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>See this agent&apos;s work</TooltipContent>
                </Tooltip>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      onClick={() => { void handleTogglePauseResume(); }}
                      disabled={agentActionBusy}
                      className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1 rounded-sm disabled:opacity-50"
                      aria-label={currentAgentPaused ? "Resume agent" : "Pause agent"}
                    >
                      {currentAgentPaused ? <Play size={16} /> : <Pause size={16} />}
                    </button>
                  </TooltipTrigger>
                  <TooltipContent>{currentAgentPaused ? "Resume agent" : "Pause agent"}</TooltipContent>
                </Tooltip>
              </div>
            )}
            <div style={{ flex: 1 }} />
            {statusIndicator}
            <Tooltip>
              <TooltipTrigger asChild>
                <button
                  onClick={() => openSupport()}
                  className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1 mr-1"
                  aria-label="Get help"
                >
                  <HelpCircle size={16} />
                </button>
              </TooltipTrigger>
              <TooltipContent>Get help / report a problem</TooltipContent>
            </Tooltip>
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
            <LaunchJoinBanner />
            <LaunchJoinModal />
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
            // No `workspace`: the backend fills the layout for the owner's
            // runtime — see useAgents.createAgent / _sanitize_agent_create.
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
      </div>
    </>
  );
}
