"use client";

// The workspace shell, at every width: one main view per place (Chat,
// Activity, Work, Library) and one right pane, plus the URL reconciler and the
// chrome (rail, sidebar, banners, stepper). Below 600 px the rail and the
// sidebar become one drawer (workspace.css), opened from the phone bar, and the
// pane a full-screen sheet. Mounted by page.tsx inside MarketplaceDrawerProvider.
import { Menu } from "lucide-react";
import { LogoMark } from "@/components/Logo";
import "./workspace.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { OrganizationList, useAuth, useOrganization } from "@clerk/nextjs";
import { useOnboardingGate } from "@/hooks/useOnboardingGate";
import { useAgents } from "@/hooks/useAgents";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { trapTab } from "@/lib/focusTrap";
import { drawerUntil } from "./paneSheet";
import { replaceSearchParams } from "@/lib/shellNav";
import { capture } from "@/lib/analytics";
import { ProvisioningStepper } from "@/components/chat/ProvisioningStepper";
import { TrialBanner } from "@/components/chat/TrialBanner";
import { OutOfCreditsBanner } from "@/components/chat/OutOfCreditsBanner";
import { LaunchJoinBanner } from "@/components/chat/LaunchJoinBanner";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
import { TeamsEventsProvider } from "@/components/teams/TeamsEventsProvider";
import { MessagingWorkspaceProvider } from "@/components/messaging/MessagingWorkspaceContext";
import { CommandPaletteProvider } from "@/components/teams/command-palette/CommandPaletteProvider";
import { preloadTeamsPanels } from "@/components/teams/TeamsPanelRouter";
import { DesktopPermissionsGate } from "@/components/desktop/DesktopPermissionsGate";
import { ChatHistorySync } from "./ChatHistorySync";
import { useIdeasFirstOpen } from "./ideasFirstOpen";
import { useUserSyncOnce } from "@/components/chat/shell/useUserSyncOnce";
import { useChatThemeAccountSync } from "@/components/chat/shell/useChatThemeAccountSync";
import { useCheckoutReturn } from "@/components/chat/shell/useCheckoutReturn";
import { useChatReady } from "@/components/chat/shell/useChatReady";
import { PANEL_COMPONENTS } from "./panelComponents";
import { ChatPanel } from "./panels/ChatPanel";
import { RightPane } from "./RightPane";
import { WorkspaceSidebar } from "./WorkspaceSidebar";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { InvitePeopleDialog } from "./InvitePeopleDialog";
import { MESSAGES_PANEL_ID, resolvePanelAlias, setSuperAgentAliases, type PanelKind } from "./panelRegistry";
import { parseCommand } from "./urlChannel";
import { deriveReconcilerAction, isCommandTargetOpen } from "./workspaceShellReconciler";
import {
  attachShell,
  openPanel,
  openChatPanel,
  openMessagePanel,
  openSessionPanel,
  closeSubjectDeleted,
  useWorkspaceState,
  getWorkspaceSnapshot,
  refreshWorkspaceSnapshot,
} from "./workspaceController";

// Only a teams DETAIL pane may swap itself for its list via leaveToList —
// same id grammar as panelRegistry.durableParamsFor.
const TEAMS_DETAIL_PANEL_ID_RE = /^teams-(issue|routine|project|goal)-detail:/;

// List panel key → its detail panel key, so a self-identifying leaveToList
// caller (list key + its own detail id) maps to the exact panel id.
const DETAIL_KEY_BY_LIST_KEY: Readonly<Record<string, string>> = {
  "teams-issues": "teams-issue-detail",
  "teams-goals": "teams-goal-detail",
  "teams-projects": "teams-project-detail",
  "teams-routines": "teams-routine-detail",
};

function DesktopMessagingProvider({ children }: { children: React.ReactNode }) {
  const { messagesVisible, browserVisible } = useWorkspaceState();
  return (
    <MessagingWorkspaceProvider subscribeToRoom={messagesVisible} observeRuns={browserVisible}>
      {children}
    </MessagingWorkspaceProvider>
  );
}

export function WorkspaceShell(): React.ReactElement {
  const onboardingPhase = useOnboardingGate();
  const { userId, isSignedIn } = useAuth();
  const { organization } = useOrganization();
  if (onboardingPhase === "selecting") {
    return (
      <main className="flex min-h-screen flex-col items-center justify-center gap-6 p-4">
        <h1 className="text-2xl font-bold">Choose your workspace</h1>
        <OrganizationList hidePersonal afterSelectOrganizationUrl="/chat" afterCreateOrganizationUrl="/onboarding" skipInvitationScreen />
      </main>
    );
  }
  if (onboardingPhase !== "ready" || !isSignedIn || !userId || !organization) {
    return (
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 12, minHeight: "100vh" }}>
        <LogoMark className="brand-mark--hero" />
        <div style={{ color: "var(--midnight-muted)", fontSize: 14 }}>Loading…</div>
      </div>
    );
  }
  const scopeId = organization.id;
  return <AuthenticatedWorkspace key={`${userId}:${scopeId}`} workspaceName={organization.name || "Lightbulb"} />;
}

/** The place's main view and the right pane. The conversation stays mounted in every place
 *  (hidden outside Chat), so full screen can fold it into the narrow chat column beside the
 *  pane with its state intact: the conversation you were in, or the CEO DM by default. */
function WorkspaceBody(): React.ReactElement {
  const { place, main, chatParams, pane, fullScreen } = useWorkspaceState();
  const Main = main ? PANEL_COMPONENTS[main.component as PanelKind] : null;
  return (
    <div className="workspace-body" data-full={fullScreen || undefined}>
      <div className="workspace-view workspace-view--chat" hidden={place !== "chat" && !fullScreen}>
        <ChatPanel id={MESSAGES_PANEL_ID} params={chatParams} />
      </div>
      {/* Settings renders outside the provisioning gate (SettingsMain), so it is never drawn here. */}
      {main && Main && main.component !== "settings" && (
        <div className="workspace-view" hidden={fullScreen}>
          <Main key={main.panelId} id={main.panelId} params={main.params} />
        </div>
      )}
      {pane && <RightPane key={pane.panelId} pane={pane} fullScreen={fullScreen} />}
    </div>
  );
}

/** Settings is reachable while the workspace is still being provisioned: the provision gate's and the trial emails'
 *  Billing and Model links land here, so it sits outside ProvisioningStepper's ready-gate (which would only draw the
 *  gate card again). */
function SettingsMain(): React.ReactElement | null {
  const { main } = useWorkspaceState();
  if (main?.component !== "settings") return null;
  const Settings = PANEL_COMPONENTS.settings;
  return <div className="workspace-view workspace-view--settings"><Settings key={main.panelId} id={main.panelId} params={main.params} /></div>;
}

function AuthenticatedWorkspace({ workspaceName }: { workspaceName: string }): React.ReactElement {
  const { activePanelId, place, pane, fullScreen } = useWorkspaceState();
  const { defaultId, isSettled: agentsSettled } = useAgents();
  const defaultIdRef = useRef<string | undefined>(defaultId);
  const agentsSettledRef = useRef(agentsSettled);
  useEffect(() => {
    defaultIdRef.current = defaultId;
    agentsSettledRef.current = agentsSettled;
  });

  const searchParams = useSearchParams();
  const lastAppliedRef = useRef<string | null>(null);
  // The slots exist while the shell is mounted. Declared BEFORE the reconciler effects so a
  // (strict-mode) remount re-attaches, and re-applies the URL command, before they run.
  useEffect(() => {
    lastAppliedRef.current = null;
    return attachShell(() => defaultIdRef.current);
  }, []);
  // Sentinel re-resolution: the roster arriving is not a slot change.
  useEffect(() => {
    refreshWorkspaceSnapshot();
  }, [defaultId]);

  const [recoveryTriggered, setRecoveryTriggered] = useState(false);
  // Phone widths: the rail + sidebar drawer; narrow windows (600-999 px): the sidebar drawer. Wider, the CSS ignores it.
  // The drawer is open only at the place it was opened: any navigation (rail,
  // sidebar, + Task, a link) moves the main view, the pane or the URL, which
  // closes it without each caller remembering to.
  const navPlace = `${activePanelId}|${pane?.panelId ?? ""}|${searchParams?.toString() ?? ""}`;
  const [navOpenAt, setNavOpenAt] = useState<string | null>(null);
  const navOpen = navOpenAt === navPlace;
  const closeNav = useCallback(() => setNavOpenAt(null), []);
  const superAgent = useSuperAgent();
  // Super-agent on a narrow window (600-999 px): the drawer is the sidebar alone, beside a rail that stays on screen.
  const [sidebarOnly, setSidebarOnly] = useState(false);
  const openNav = () => {
    setSidebarOnly(superAgent && !!window.matchMedia?.("(min-width: 600px)")?.matches);
    setNavOpenAt(navPlace);
  };
  const shellRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const drawerParts = () => {
    const shell = shellRef.current;
    return sidebarOnly ? [shell?.querySelector(".cream-sidebar")] : [shell?.querySelector(".workspace-rail"), shell?.querySelector(".cream-sidebar")];
  };
  useEffect(() => {
    if (!navOpen) return;
    // Keyboard and switch users land in the drawer, not behind its backdrop
    // (the main column is `inert` while it is open), and return to the menu button.
    // The first ENABLED control: Back is first in the rail and is disabled with no history.
    const shell = shellRef.current;
    shell?.querySelector<HTMLElement>(sidebarOnly ? ".cream-sidebar button:not(:disabled)" : ".workspace-rail button:not(:disabled)")?.focus();
    // Modal: the rail beside a sidebar-only drawer is out of reach too.
    const rail = sidebarOnly ? shell?.querySelector<HTMLElement>(".workspace-rail") : null;
    rail?.setAttribute("inert", "");
    const menuButton = menuButtonRef.current;
    // Widening past the drawer breakpoint hides the drawer's backdrop, so
    // close it there rather than leave the main column inert with no way out.
    const wide = window.matchMedia?.(`(min-width: ${drawerUntil(superAgent)}px)`);
    wide?.addEventListener("change", closeNav);
    // Narrowing to a phone turns a sidebar-only drawer into the phone drawer (rail and sidebar): the rail is back in it.
    const tablet = sidebarOnly ? window.matchMedia?.("(min-width: 600px)") : undefined;
    const toPhone = () => { if (!tablet?.matches) setSidebarOnly(false); };
    tablet?.addEventListener("change", toPhone);
    return () => {
      wide?.removeEventListener("change", closeNav);
      tablet?.removeEventListener("change", toPhone);
      rail?.removeAttribute("inert");
      menuButton?.focus();
    };
  }, [navOpen, closeNav, sidebarOnly, superAgent]);

  useUserSyncOnce(true);
  useChatThemeAccountSync(true);
  const { showSubscriptionSuccess } = useCheckoutReturn();
  const { chatReady } = useChatReady();
  const { userId: viewerId, orgId } = useAuth();

  // [reconciler] — pure decision in workspaceShellReconciler; this executes it.
  // Ordering (pinned by workspaceShellUrl.test.ts): signature latch BEFORE
  // parse; latch value is the POST-strip signature so the replaceSearchParams
  // churn below never re-executes the command.
  const runReconciler = useCallback((search: string) => {
    const snapshot = getWorkspaceSnapshot();
    if (!snapshot.ready) return;
    // Chat commands are deferred (null decision — nothing latched, nothing
    // stripped) while the agent roster is unsettled; the isSettled effect
    // below re-runs this once the default id is knowable.
    const decision = deriveReconcilerAction(search, lastAppliedRef.current, agentsSettledRef.current);
    if (!decision) return;
    lastAppliedRef.current = decision.nextSignature;
    const { command } = decision;
    if (!command) return;
    const wasOpen = isCommandTargetOpen(command, snapshot.openPanelIds);
    if (command.descriptor.component === "chat") {
      // Room permalinks (?panel=chat&room=…&message=…) carry roomId, not agentId.
      if (command.descriptor.params.sessionId) {
        openSessionPanel(command.descriptor.params.sessionId, Number(command.descriptor.params.seq) || undefined);
      } else if (command.descriptor.params.roomId) {
        openMessagePanel(command.descriptor.params.roomId, command.descriptor.params.messageId);
      } else {
        openChatPanel(command.descriptor.params.agentId);
      }
    } else if (decision.panelKey) {
      // NO pushUrl: the URL already carries this command.
      openPanel(decision.panelKey, decision.durableParams, { oneShots: command.oneShots });
    }
    // The pane the URL names beside its place (a reload restores both).
    if (command.pane) openPanel(command.pane.key, command.pane.params);
    // Analytics reports the panel that actually opened, so a retired alias is
    // resolved HERE — decision.panelKey stays raw because describePanel needs
    // the pre-alias key to pick the right view (see the reconciler's doc).
    if (!wasOpen) {
      capture("workspace_panel_opened", {
        panel: decision.panelKey ? resolvePanelAlias(decision.panelKey) : "chat",
      });
    }
    if (decision.strippedSearch !== null) {
      replaceSearchParams(new URLSearchParams(decision.strippedSearch));
    }
  }, []);

  // Super-agent (UX slice 4): Work's keys open their new homes (panelRegistry's aliases), and /chat with nothing
  // named lands on Home, not a conversation. The switch arrives with the live config, often after the first
  // reconcile, so the URL is reconciled again once it does.
  useEffect(() => {
    setSuperAgentAliases(superAgent);
    if (!superAgent || !getWorkspaceSnapshot().ready) return;
    lastAppliedRef.current = null;
    runReconciler(window.location.search);
    const { place, chatParams } = getWorkspaceSnapshot();
    if (!parseCommand(window.location.search) && place === "chat" && !chatParams.roomId && !chatParams.sessionId) {
      openPanel("home");
      // Keep what the URL carries (a Stripe or OAuth return: ?provider=…&checkout=…); only `panel` is set.
      const sp = new URLSearchParams(window.location.search);
      sp.set("panel", "home");
      replaceSearchParams(sp);
    }
    return () => setSuperAgentAliases(false);
  }, [superAgent, runReconciler]);

  useEffect(() => {
    // searchParams is the re-run TRIGGER only. Reconcile against the LIVE
    // window.location.search: a strip via replaceSearchParams (History API)
    // lands before Next's searchParams update, so this sees the post-strip URL.
    runReconciler(window.location.search);
  }, [searchParams, runReconciler]);

  // A chat command that arrived while the roster was unsettled was deferred
  // (not latched) — re-run the reconciler once useAgents settles.
  useEffect(() => {
    if (agentsSettled) runReconciler(window.location.search);
  }, [agentsSettled, runReconciler]);

  // After the reconciler: a URL that names a pane keeps it, and Ideas does not replace it. Super-agent: the first run's
  // ideas wait in the Lightbulb DM instead (UX slice 6), so the pane never opens by itself.
  useIdeasFirstOpen(chatReady && !superAgent, viewerId, orgId ?? viewerId);

  // [selectAgent listener] — the shell only LISTENS, never dispatches
  // (loop-free by construction; accepts MarketplaceToast's sourceless events
  // AND RunNotifications' source:"notification").
  useEffect(() => {
    function onSelectAgent(e: Event) {
      const detail = (e as CustomEvent<{ agentId?: string }>).detail;
      if (detail?.agentId) openChatPanel(detail.agentId);
    }
    window.addEventListener("selectAgent", onSelectAgent);
    return () => window.removeEventListener("selectAgent", onSelectAgent);
  }, []);

  // Warm the code-split Teams panel chunks while the browser is idle so the
  // first open of a Teams panel mounts from the module cache instead of
  // fetching its chunk on click.
  useEffect(() => {
    // Safari has no requestIdleCallback — fall back to a short timer.
    if (typeof window.requestIdleCallback === "function") {
      const id = window.requestIdleCallback(() => preloadTeamsPanels(), { timeout: 5000 });
      return () => window.cancelIdleCallback(id);
    }
    const id = window.setTimeout(preloadTeamsPanels, 3000);
    return () => window.clearTimeout(id);
  }, []);

  const navValue = useMemo(
    () => ({
      // A detail opens in the right pane (openPanel routes it there).
      openDetail: (panelKey: string, id: string) =>
        openPanel(`teams-${panelKey}`, { detailId: id }, { pushUrl: true }),
      leaveToList: (listPanelKey: string, selfDetailId?: string) => {
        // Prefer the caller's self-identification; the fallback is whichever
        // detail the pane shows.
        if (selfDetailId) {
          const detailKey = Object.prototype.hasOwnProperty.call(DETAIL_KEY_BY_LIST_KEY, listPanelKey)
            ? DETAIL_KEY_BY_LIST_KEY[listPanelKey]
            : undefined;
          if (detailKey) {
            closeSubjectDeleted(`${detailKey}:${selfDetailId}`, listPanelKey);
            return;
          }
        }
        // Guard on the id grammar so a stray call from a non-detail surface
        // can't close an unrelated pane.
        const selfPanelId = getWorkspaceSnapshot().pane?.panelId;
        if (!selfPanelId || !TEAMS_DETAIL_PANEL_ID_RE.test(selfPanelId)) return;
        closeSubjectDeleted(selfPanelId, listPanelKey);
      },
    }),
    [],
  );

  // Shared by both stepper branches: same element → the body never remounts
  // when recoveryTriggered flips (prop-switch ternary, NO key).
  const body = <WorkspaceBody />;

  return (
    <InlineTeamsNavProvider value={navValue}>
      <TeamsEventsProvider>
        <DesktopMessagingProvider>
          <CommandPaletteProvider>
            <DesktopPermissionsGate />
            <ChatHistorySync />
            <InvitePeopleDialog />
            <div className="workspace-shell" ref={shellRef} data-nav-open={navOpen}
              // Without the super-agent only Chat has a sidebar; with it, every place does (UX slice 4). Full screen folds it
              // into the chat column.
              data-sidebar={(superAgent || place === "chat") && !fullScreen} data-super-agent={superAgent || undefined}
              onKeyDown={navOpen ? (e) => { if (e.key === "Escape") closeNav(); else trapTab(e, drawerParts()); } : undefined}>
              {/* Before the drawer in the DOM, so the drawer paints over it. */}
              <div className="workspace-nav-backdrop" onClick={closeNav} />
              <WorkspaceHeader chatReady={chatReady} onNavigate={closeNav} />
              <WorkspaceSidebar
                id="workspace-sidebar" dialog={navOpen && sidebarOnly}
                chatReady={chatReady}
                onRecoveryReprovision={() => setRecoveryTriggered(true)}
                onNavigate={closeNav}
              />
              <div className="workspace-main" inert={navOpen}>
                <div className="workspace-phone-bar">
                  <button type="button" ref={menuButtonRef} aria-label="Open menu" aria-expanded={navOpen} aria-controls="workspace-sidebar" onClick={openNav}>
                    <Menu size={20} aria-hidden="true" />
                  </button>
                  <span className="workspace-name">{workspaceName}</span>
                </div>
                <div className="workspace-content">
                  <TrialBanner />
                  <OutOfCreditsBanner />
                  <LaunchJoinBanner />
                  {showSubscriptionSuccess && (
                    <div className="subscription-banner">
                      <p>Subscription confirmed! Your agent is being upgraded.</p>
                    </div>
                  )}
                  {/* Hidden, never unmounted, while Settings shows: the conversation keeps its state. */}
                  <div className="workspace-gated" hidden={place === "settings"}>
                    {recoveryTriggered ? (
                      <ProvisioningStepper trigger="recovery">{body}</ProvisioningStepper>
                    ) : (
                      <ProvisioningStepper>{body}</ProvisioningStepper>
                    )}
                  </div>
                  <SettingsMain />
                </div>
              </div>
            </div>
          </CommandPaletteProvider>
        </DesktopMessagingProvider>
      </TeamsEventsProvider>
    </InlineTeamsNavProvider>
  );
}
