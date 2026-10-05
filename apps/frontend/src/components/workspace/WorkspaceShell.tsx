"use client";

// The dockable-workspace shell, at every width: dock host + URL reconciler +
// layout persistence + chrome (rail, sidebar, banners, stepper). Below 600 px
// the rail and the sidebar become one drawer (workspace.css), opened from the
// phone bar. Mounted by page.tsx inside MarketplaceDrawerProvider.
import "dockview-react/dist/styles/dockview.css";
import { Menu } from "lucide-react";
import { LogoMark } from "@/components/Logo";
import "./workspace.css";
import { constrainGroupHeight } from "./groupConstraints";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { OrganizationList, useAuth, useOrganization } from "@clerk/nextjs";
import {
  DockviewReact,
  themeLight,
  type DockviewReadyEvent,
  type DockviewTheme,
} from "dockview-react";
import { useOnboardingGate } from "@/hooks/useOnboardingGate";
import { useAgents } from "@/hooks/useAgents";
import { replaceSearchParams } from "@/lib/shellNav";
import { capture } from "@/lib/analytics";
import { ProvisioningStepper } from "@/components/chat/ProvisioningStepper";
import { TrialBanner } from "@/components/chat/TrialBanner";
import { OutOfCreditsBanner } from "@/components/chat/OutOfCreditsBanner";
import { LaunchJoinBanner } from "@/components/chat/LaunchJoinBanner";
import { LaunchJoinModal } from "@/components/chat/LaunchJoinModal";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
import { TeamsEventsProvider } from "@/components/teams/TeamsEventsProvider";
import { MessagingWorkspaceProvider } from "@/components/messaging/MessagingWorkspaceContext";
import { CommandPaletteProvider } from "@/components/teams/command-palette/CommandPaletteProvider";
import { preloadTeamsPanels } from "@/components/teams/TeamsPanelRouter";
import { DesktopPermissionsGate } from "@/components/desktop/DesktopPermissionsGate";
import { ChatHistorySync } from "./ChatHistorySync";
import { useUserSyncOnce } from "@/components/chat/shell/useUserSyncOnce";
import { useChatThemeAccountSync } from "@/components/chat/shell/useChatThemeAccountSync";
import { useCheckoutReturn } from "@/components/chat/shell/useCheckoutReturn";
import { useChatReady } from "@/components/chat/shell/useChatReady";
import { PANEL_COMPONENTS } from "./panelComponents";
import { WorkspaceSidebar } from "./WorkspaceSidebar";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { TasksHeaderActions } from "./TasksHeaderActions";
import { buildMissionControlLayout } from "./buildDefaultLayout";
import { readLayout, writeLayout } from "./layoutStorage";
import { clearClosedPanelFromUrl } from "./urlChannel";
import { MESSAGES_PANEL_ID, resolvePanelAlias } from "./panelRegistry";
import { deriveReconcilerAction, isCommandTargetOpen } from "./workspaceShellReconciler";
import {
  attachDockviewApi,
  openPanel,
  openChatPanel,
  openMessagePanel,
  openSessionPanel,
  closeSubjectDeleted,
  refitSplits,
  isShellOperationInProgress,
  useWorkspaceState,
  getWorkspaceSnapshot,
  refreshWorkspaceSnapshot,
} from "./workspaceController";

// Brand dock theme. `className` is a SPACE-SEPARATED list (dockview's
// Classnames helper splits it and toggles each on `.dv-shell`), so keeping
// dockview's own `dockview-theme-light` alongside ours means the stock
// stylesheet still supplies every `--dv-*` default and workspace.css's
// `.dv-shell.dockview-theme-lightbulb` block only repoints the ~18 that
// carry colour, tone or rhythm.
const LIGHTBULB_THEME: DockviewTheme = {
  ...themeLight,
  name: "lightbulb",
  className: `${themeLight.className} dockview-theme-lightbulb`,
  gap: 4,
};

// Only a teams DETAIL panel may swap itself for its list via leaveToList —
// same id grammar as panelRegistry.durableParamsFor.
const TEAMS_DETAIL_PANEL_ID_RE = /^teams-(issue|routine|project|goal)-detail:/;

// List panel key → its detail panel key, so a self-identifying leaveToList
// caller (list key + its own detail id) maps to the exact dock panel id
// without trusting dock focus.
const DETAIL_KEY_BY_LIST_KEY: Readonly<Record<string, string>> = {
  "teams-issues": "teams-issue-detail",
  "teams-goals": "teams-goal-detail",
  "teams-projects": "teams-project-detail",
  "teams-routines": "teams-routine-detail",
};

// The recovery regression unmounts the dock mid-session (ProvisioningStepper
// swaps its children for the loader) without unmounting WorkspaceShell, so the
// shell's own effect cleanups never run. This sentinel rides INSIDE the dock
// element — its unmount cleanup IS the dock's unmount — and is rendered BEFORE
// DockviewReact so its cleanup (flush + dispose + detach) runs while the
// dockview api is still alive.
function DockUnmountSentinel({ onUnmount }: { onUnmount: () => void }): null {
  const ref = useRef(onUnmount);
  useEffect(() => {
    ref.current = onUnmount;
  });
  useEffect(() => () => ref.current(), []);
  return null;
}

function DesktopMessagingProvider({ children }: { children: React.ReactNode }) {
  const { messagesVisible, openPanelIds } = useWorkspaceState();
  return (
    <MessagingWorkspaceProvider subscribeToRoom={messagesVisible} observeRuns={openPanelIds.includes("browser")}>
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
  return <AuthenticatedWorkspace key={`${userId}:${scopeId}`} scopeId={scopeId} workspaceName={organization.name || "Lightbulb"} />;
}

function AuthenticatedWorkspace({ scopeId, workspaceName }: { scopeId: string; workspaceName: string }): React.ReactElement {
  const { activePanelId, teamsPanelVisible } = useWorkspaceState();
  const { agents, defaultId, isSettled: agentsSettled } = useAgents();
  const defaultIdRef = useRef<string | undefined>(defaultId);
  const agentsSettledRef = useRef(agentsSettled);
  useEffect(() => {
    defaultIdRef.current = defaultId;
    agentsSettledRef.current = agentsSettled;
  });
  // Roster for the layout restore's dead-panel prune. A value, not a ref, and
  // it rides in onReady's deps: dockview's onReady fires from the dock's own
  // mount effect (a CHILD of this component), which runs BEFORE this
  // component's effects — so a ref synced in an effect would still be empty at
  // the one moment the restore reads it. Closing over the mount render's value
  // is what's wanted. Re-creating onReady later is inert: dockview reads the
  // prop once, in a `[]`-deps mount effect. Empty while unsettled is the
  // contract sanitizeLayout expects ("roster unknown" → prune nothing).
  const loadedAgentIds = useMemo(
    () => (agentsSettled ? agents.map((a) => a.id) : []),
    [agentsSettled, agents],
  );
  // Sentinel re-resolution: the snapshot resolves chat:__default__ through
  // defaultIdRef at recompute time, and the roster arriving is not a dockview
  // event — without this, a cold load leaves focusedChatAgentId /
  // visibleChatAgentIds null (no sidebar highlight, toast
  // suppression misses the watched run) until some unrelated layout event.
  // Declared AFTER the ref-sync effect above so the ref is fresh when it runs.
  useEffect(() => {
    refreshWorkspaceSnapshot();
  }, [defaultId]);

  const searchParams = useSearchParams();
  const lastAppliedRef = useRef<string | null>(null);
  const [recoveryTriggered, setRecoveryTriggered] = useState(false);
  // Phone widths only: the rail + sidebar drawer. Wider, the CSS ignores it.
  const [navOpen, setNavOpen] = useState(false);
  const closeNav = useCallback(() => setNavOpen(false), []);
  const shellRef = useRef<HTMLDivElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!navOpen) return;
    // Keyboard and switch users land in the drawer, not behind its backdrop
    // (the pane is `inert` while it is open), and return to the menu button.
    // The first ENABLED control: Back is first in the rail and is disabled with no history.
    shellRef.current?.querySelector<HTMLElement>(".workspace-rail button:not(:disabled)")?.focus();
    const menuButton = menuButtonRef.current;
    // Widening past the phone breakpoint hides the drawer's backdrop, so
    // close it there rather than leave the pane inert with no way out.
    const wide = window.matchMedia?.("(min-width: 600px)");
    wide?.addEventListener("change", closeNav);
    return () => {
      wide?.removeEventListener("change", closeNav);
      menuButton?.focus();
    };
  }, [navOpen, closeNav]);

  useUserSyncOnce(true);
  useChatThemeAccountSync(true);
  const { showSubscriptionSuccess } = useCheckoutReturn();
  const { chatReady } = useChatReady();

  // [BODY 3: reconciler] — pure decision in workspaceShellReconciler; this
  // executes it. Ordering (pinned by workspaceShellUrl.test.ts): signature
  // latch BEFORE parse; latch value is the POST-strip signature so the
  // replaceSearchParams churn below never re-executes the command.
  const runReconciler = useCallback((search: string) => {
    // No dock, no reconcile: while the dock is unmounted (initial gate
    // render, recovery regression) executing would silently drop the command
    // AND the strip below would eat one-shots (?new=task) before any panel
    // exists to receive them. onReady re-runs this after attaching (with the
    // latch reset), so the pending URL command applies then.
    const snapshot = getWorkspaceSnapshot();
    if (!snapshot.ready) return;
    // Chat commands are deferred (null decision — nothing latched, nothing
    // stripped) while the agent roster is unsettled; the isSettled effect
    // below re-runs this once the default id is knowable.
    const decision = deriveReconcilerAction(
      search,
      lastAppliedRef.current,
      agentsSettledRef.current,
    );
    if (!decision) return;
    lastAppliedRef.current = decision.nextSignature;
    const { command } = decision;
    if (!command) return;
    const wasOpen = isCommandTargetOpen(
      command,
      snapshot.openPanelIds,
    );
    if (command.descriptor.component === "chat") {
      // Room permalinks (?panel=chat&room=…&message=…) carry roomId, not
      // agentId — openChatPanel no-ops on an undefined agentId, so route
      // those through openMessagePanel instead.
      if (command.descriptor.params.sessionId) {
        openSessionPanel(command.descriptor.params.sessionId);
      } else if (command.descriptor.params.roomId) {
        openMessagePanel(command.descriptor.params.roomId, command.descriptor.params.messageId);
      } else {
        openChatPanel(command.descriptor.params.agentId);
      }
    } else if (decision.panelKey) {
      // NO pushUrl: the URL already carries this command.
      openPanel(decision.panelKey, decision.durableParams, { oneShots: command.oneShots });
    }
    // Analytics reports the panel that actually opened, so a retired alias is
    // resolved HERE — decision.panelKey stays raw because describePanel needs
    // the pre-alias key to pick the right tab (see the reconciler's doc).
    if (!wasOpen) {
      capture("workspace_panel_opened", {
        panel: decision.panelKey ? resolvePanelAlias(decision.panelKey) : "chat",
      });
    }
    if (decision.strippedSearch !== null) {
      replaceSearchParams(new URLSearchParams(decision.strippedSearch));
    }
  }, []);

  useEffect(() => {
    // searchParams is the re-run TRIGGER only. Reconcile against the LIVE
    // window.location.search, not the Next-parsed searchParams: onReady strips
    // the URL synchronously via replaceSearchParams (History API) BEFORE Next's
    // searchParams updates, so reading window.location here makes that strip
    // visible to this effect. A warm first mount then latches out (post-strip
    // search vs the post-strip signature) instead of re-executing — and re-
    // opening — the URL command. (Task 11 review handoff.)
    runReconciler(window.location.search);
  }, [searchParams, runReconciler]);

  // A chat command that arrived while the roster was unsettled was deferred
  // (not latched) — re-run the reconciler once useAgents settles so the
  // pending URL command executes against a knowable default agent id.
  // Declared AFTER the ref-sync effect above so agentsSettledRef is fresh.
  useEffect(() => {
    if (agentsSettled) runReconciler(window.location.search);
  }, [agentsSettled, runReconciler]);

  // [BODY 1 + 2: onReady — attach, restore, URL command, clear-on-close,
  // persistence] with the teardown mirrored in the dock-unmount sentinel.
  const detachRef = useRef<(() => void) | null>(null);
  const disposablesRef = useRef<Array<{ dispose(): void }>>([]);
  const flushRef = useRef<(() => void) | null>(null);

  const onReady = useCallback(
    (event: DockviewReadyEvent) => {
      const api = event.api;
      detachRef.current = attachDockviewApi(api, () => defaultIdRef.current);

      // Restore the persisted layout, or build Mission Control. A corrupt
      // blob that survives the sanitizer still must not brick the dock.
      // Chat panels for agents that no longer exist (post-wipe / reprovision)
      // are pruned here when the roster is already warm. On a cold load it
      // isn't, and the restore keeps them — ChatPanel's agent-gone fallback
      // covers that until the next restore.
      const saved = readLayout(scopeId, loadedAgentIds);
      if (saved) {
        try {
          api.fromJSON(saved);
        } catch {
          api.clear();
          buildMissionControlLayout(api);
        }
      } else {
        buildMissionControlLayout(api);
      }
      // A split saved on a wide window must not come back on a narrow one.
      // Next frame: the dock has no width until it has laid out.
      requestAnimationFrame(refitSplits);

      // Floor every group's height so none can be crushed to its tab bar —
      // both the ones we just restored/built and any added later. See
      // groupConstraints.ts for why this is two operations, not one.
      api.groups.forEach((group) => constrainGroupHeight(group));
      disposablesRef.current.push(
        api.onDidAddGroup((group) => constrainGroupHeight(group)),
      );

      // URL wins focus (spec §6 rule 4): execute the current URL command on
      // top of the restore. Reset the latch first — commands latched while
      // the dock was unmounted (initial gate render, recovery regression)
      // were dropped by the null-api guard and must re-apply now.
      lastAppliedRef.current = null;
      runReconciler(window.location.search);

      // Clear-on-close: closing the URL-named panel canonicalizes the URL in
      // place (replace, never push) so refresh can't resurrect it.
      disposablesRef.current.push(
        api.onDidRemovePanel((panel) => {
          if (isShellOperationInProgress()) return;
          const cleared = clearClosedPanelFromUrl(
            window.location.search,
            panel.id,
          );
          if (cleared !== null) replaceSearchParams(new URLSearchParams(cleared));
        }),
      );

      // Persistence: debounced (500 ms) writes; flush on pagehide, on the
      // tab going hidden, and on dock unmount (sentinel below).
      let timer: number | null = null;
      const write = () => {
        timer = null;
        try {
          writeLayout(scopeId, api.toJSON());
        } catch {
          // api mid-teardown — losing the last ≤500ms of layout is fine
        }
      };
      disposablesRef.current.push(
        api.onDidLayoutChange(() => {
          if (timer !== null) window.clearTimeout(timer);
          timer = window.setTimeout(write, 500);
        }),
      );
      const flush = () => {
        if (timer === null) return;
        window.clearTimeout(timer);
        write();
      };
      flushRef.current = flush;
      const onPageHide = () => flush();
      const onVisibilityChange = () => {
        if (document.visibilityState === "hidden") flush();
      };
      window.addEventListener("pagehide", onPageHide);
      document.addEventListener("visibilitychange", onVisibilityChange);
      disposablesRef.current.push({
        dispose: () => {
          window.removeEventListener("pagehide", onPageHide);
          document.removeEventListener("visibilitychange", onVisibilityChange);
          if (timer !== null) window.clearTimeout(timer);
        },
      });
    },
    [scopeId, runReconciler, loadedAgentIds],
  );

  // Dock teardown (unmount OR shell unmount): flush the pending write while
  // the api is alive, dispose every listener, detach the controller.
  const teardownDock = useCallback(() => {
    flushRef.current?.();
    flushRef.current = null;
    disposablesRef.current.forEach((d) => d.dispose());
    disposablesRef.current = [];
    detachRef.current?.();
    detachRef.current = null;
  }, []);

  // [BODY 4: selectAgent listener] — the shell only LISTENS, never dispatches
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
      openDetail: (panelKey: string, id: string) =>
        openPanel(`teams-${panelKey}`, { detailId: id }, { pushUrl: true }),
      leaveToList: (listPanelKey: string, selfDetailId?: string) => {
        // Prefer the caller's self-identification: dock focus can move
        // between clicking Delete and confirming (the confirm dialog is a
        // portal — peeking another tab meanwhile is easy), so the ACTIVE
        // panel is not reliably the invoking detail panel.
        if (selfDetailId) {
          const detailKey = Object.prototype.hasOwnProperty.call(
            DETAIL_KEY_BY_LIST_KEY,
            listPanelKey,
          )
            ? DETAIL_KEY_BY_LIST_KEY[listPanelKey]
            : undefined;
          if (detailKey) {
            closeSubjectDeleted(`${detailKey}:${selfDetailId}`, listPanelKey);
            return;
          }
        }
        // Fallback (no self id): the invoking detail panel is assumed to be
        // the dock's active panel. Read the live snapshot at call time rather
        // than subscribing — the shell has no other reactive use for it, and a
        // subscription here would re-render the whole shell (dock included) on
        // every dockview event. Guard on the id grammar so a stray call from a
        // non-detail surface can't close an unrelated panel.
        const selfPanelId = getWorkspaceSnapshot().activePanelId;
        if (!selfPanelId || !TEAMS_DETAIL_PANEL_ID_RE.test(selfPanelId)) return;
        closeSubjectDeleted(selfPanelId, listPanelKey);
      },
    }),
    [],
  );

  // Shared by both stepper branches: same element → the dock never remounts
  // when recoveryTriggered flips (prop-switch ternary, NO key).
  const dock = (
    <div className="workspace-dock">
      <DockUnmountSentinel onUnmount={teardownDock} />
      <DockviewReact
        components={PANEL_COMPONENTS}
        theme={LIGHTBULB_THEME}
        onReady={onReady}
        disableFloatingGroups
        rightHeaderActionsComponent={TasksHeaderActions}
        // Tabs only. This kills BOTH drag backends at the source
        // (resolveDndCapabilities returns html5:false + pointer:false), which
        // is why it is one option rather than four handler removals: tab
        // reorder, cross-group drags, dock-edge drops and the group-void drag
        // handle all go together, and a dockview upgrade cannot quietly
        // reintroduce one of them.
        //
        // Users cannot split by dragging. The one split is programmatic: Tasks
        // opens beside Messages (workspaceController.addOrFocus), and its
        // sash still resizes because sashes are not a drag-and-drop backend.
        disableDnd
      />
    </div>
  );

  return (
    <InlineTeamsNavProvider value={navValue}>
      <TeamsEventsProvider>
        <DesktopMessagingProvider>
          <CommandPaletteProvider>
            <DesktopPermissionsGate />
            <ChatHistorySync />
            <div className="workspace-shell" ref={shellRef} data-nav-open={navOpen}
              onKeyDown={navOpen ? (e) => { if (e.key === "Escape") closeNav(); } : undefined}
              data-messages-active={
              // Messages hides the dock tab bars only when it is alone on
              // screen. With Tasks split beside it, both groups keep their
              // headers so Tasks' close and Expand controls stay reachable.
              activePanelId === MESSAGES_PANEL_ID && !teamsPanelVisible
            }>
              {/* Before the drawer in the DOM, so the drawer paints over it. */}
              <div className="workspace-nav-backdrop" onClick={closeNav} />
              <WorkspaceHeader chatReady={chatReady} onNavigate={closeNav} />
              <WorkspaceSidebar
                chatReady={chatReady}
                onRecoveryReprovision={() => setRecoveryTriggered(true)}
                onNavigate={closeNav}
              />
              <div className="workspace-main" inert={navOpen}>
                <div className="workspace-phone-bar">
                  <button type="button" ref={menuButtonRef} aria-label="Open menu" aria-expanded={navOpen} onClick={() => setNavOpen(true)}>
                    <Menu size={20} aria-hidden="true" />
                  </button>
                  <span className="workspace-name">{workspaceName}</span>
                </div>
                <div className="workspace-content">
                  <TrialBanner />
                  <OutOfCreditsBanner />
                  <LaunchJoinBanner />
                  <LaunchJoinModal />
                  {showSubscriptionSuccess && (
                    <div className="subscription-banner">
                      <p>Subscription confirmed! Your agent is being upgraded.</p>
                    </div>
                  )}
                  {recoveryTriggered ? (
                    <ProvisioningStepper trigger="recovery">{dock}</ProvisioningStepper>
                  ) : (
                    <ProvisioningStepper>{dock}</ProvisioningStepper>
                  )}
                </div>
              </div>
            </div>
          </CommandPaletteProvider>
        </DesktopMessagingProvider>
      </TeamsEventsProvider>
    </InlineTeamsNavProvider>
  );
}
