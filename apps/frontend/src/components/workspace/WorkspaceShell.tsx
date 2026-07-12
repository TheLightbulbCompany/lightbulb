"use client";

// The desktop dockable-workspace shell: dock host + URL reconciler + layout
// persistence + chrome (sidebar, header, banners, stepper, Get Started).
// Mounted by page.tsx inside MarketplaceDrawerProvider > GatewayProvider >
// ExecApprovalsProvider (Task 13); mobile keeps ChatLayout.
import "dockview-react/dist/styles/dockview.css";
import "./workspace.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useAuth, useOrganization } from "@clerk/nextjs";
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
import { GetStarted } from "@/components/getstarted/GetStarted";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
import { TeamsEventsProvider } from "@/components/teams/TeamsEventsProvider";
import { CommandPaletteProvider } from "@/components/teams/command-palette/CommandPaletteProvider";
import { preloadTeamsPanels } from "@/components/teams/TeamsPanelRouter";
import { DesktopPermissionsGate } from "@/components/desktop/DesktopPermissionsGate";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { useUserSyncOnce } from "@/components/chat/shell/useUserSyncOnce";
import { useCheckoutReturn } from "@/components/chat/shell/useCheckoutReturn";
import { useGetStartedDerived } from "@/components/chat/shell/useGetStartedDerived";
import { PANEL_COMPONENTS } from "./panelComponents";
import { WorkspaceSidebar } from "./WorkspaceSidebar";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { buildMissionControlLayout } from "./buildDefaultLayout";
import { readLayout, writeLayout, clearLayout } from "./layoutStorage";
import { clearClosedPanelFromUrl } from "./urlChannel";
import { requestFocusComposer } from "@/lib/focusComposerRequest";
import { deriveReconcilerAction, isCommandTargetOpen } from "./workspaceShellReconciler";
import {
  attachDockviewApi,
  openPanel,
  openChatPanel,
  closeSubjectDeleted,
  isShellOperationInProgress,
  resetWorkspaceLayout,
  getWorkspaceSnapshot,
  refreshWorkspaceSnapshot,
  useWorkspaceState,
} from "./workspaceController";

const ISOL8_THEME: DockviewTheme = { ...themeLight, name: "isol8", gap: 4 };

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

export function WorkspaceShell(): React.ReactElement {
  const onboardingPhase = useOnboardingGate(); // exactly ONE mount (router side effects)
  const { userId, isSignedIn } = useAuth();
  const { organization } = useOrganization();
  const scopeId = organization?.id ?? userId ?? "anon"; // gate below makes "anon" unreachable
  const { defaultId, isSettled: agentsSettled } = useAgents();
  const defaultIdRef = useRef<string | undefined>(defaultId);
  const agentsSettledRef = useRef(agentsSettled);
  useEffect(() => {
    defaultIdRef.current = defaultId;
    agentsSettledRef.current = agentsSettled;
  });
  // Sentinel re-resolution: the snapshot resolves chat:__default__ through
  // defaultIdRef at recompute time, and the roster arriving is not a dockview
  // event — without this, a cold load leaves focusedChatAgentId /
  // visibleChatAgentIds null (no sidebar highlight, no SessionList, toast
  // suppression misses the watched run) until some unrelated layout event.
  // Declared AFTER the ref-sync effect above so the ref is fresh when it runs.
  useEffect(() => {
    refreshWorkspaceSnapshot();
  }, [defaultId]);

  const searchParams = useSearchParams();
  const lastAppliedRef = useRef<string | null>(null);
  const [recoveryTriggered, setRecoveryTriggered] = useState(false);

  // leaveToList resolves the calling detail panel from the dock's active
  // panel; via a ref so the memoized nav value never sees a stale closure.
  const { activePanelId } = useWorkspaceState();
  const activePanelIdRef = useRef(activePanelId);
  useEffect(() => {
    activePanelIdRef.current = activePanelId;
  });

  useUserSyncOnce(Boolean(isSignedIn) && onboardingPhase === "ready");
  const { showSubscriptionSuccess } = useCheckoutReturn();
  // `chatReady` + the four completion signals (chat/hire/channel/task) are
  // derived in the shared shell hook — see its doc comment for the
  // readiness-gate rationale.
  const { chatReady, ...getStartedDerived } = useGetStartedDerived();
  const { open: openMarketplace } = useMarketplaceDrawer();

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
      () => defaultIdRef.current,
    );
    if (command.descriptor.component === "chat") {
      openChatPanel(command.descriptor.params.agentId);
    } else if (decision.panelKey) {
      // NO pushUrl: the URL already carries this command.
      openPanel(decision.panelKey, decision.durableParams, { oneShots: command.oneShots });
    }
    if (!wasOpen) capture("workspace_panel_opened", { panel: decision.panelKey ?? "chat" });
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
      const saved = readLayout(scopeId);
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

      // URL wins focus (spec §6 rule 4): execute the current URL command on
      // top of the restore. Reset the latch first — commands latched while
      // the dock was unmounted (initial gate render, recovery regression)
      // were dropped by the null-api guard and must re-apply now.
      lastAppliedRef.current = null;
      runReconciler(window.location.search);

      // Clear-on-close: closing the URL-named panel canonicalizes the URL in
      // place (replace, never push) so refresh can't resurrect it. Sentinel-
      // unified: closing chat:__default__ while the URL names the concrete
      // default agent (and vice versa) still clears.
      disposablesRef.current.push(
        api.onDidRemovePanel((panel) => {
          if (isShellOperationInProgress()) return;
          const cleared = clearClosedPanelFromUrl(
            window.location.search,
            panel.id,
            () => defaultIdRef.current,
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
    [scopeId, runReconciler],
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

  const handleResetLayout = useCallback(() => {
    resetWorkspaceLayout(buildMissionControlLayout);
    clearLayout(scopeId);
    capture("workspace_layout_reset");
  }, [scopeId]);

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
        // the dock's active panel. Guard on the id grammar so a stray call
        // from a non-detail surface can't close an unrelated panel.
        const selfPanelId = activePanelIdRef.current;
        if (!selfPanelId || !TEAMS_DETAIL_PANEL_ID_RE.test(selfPanelId)) return;
        closeSubjectDeleted(selfPanelId, listPanelKey);
      },
    }),
    [],
  );

  if (onboardingPhase !== "ready") {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", minHeight: "100vh" }}>
        <div style={{ color: "#6b6b6b", fontSize: 14 }}>Loading…</div>
      </div>
    );
  }

  // Shared by both stepper branches: same element → the dock never remounts
  // when recoveryTriggered flips (prop-switch ternary, NO key).
  const dock = (
    <div className="workspace-dock">
      <DockUnmountSentinel onUnmount={teardownDock} />
      <DockviewReact
        components={PANEL_COMPONENTS}
        theme={ISOL8_THEME}
        onReady={onReady}
        disableFloatingGroups
      />
    </div>
  );

  return (
    <InlineTeamsNavProvider value={navValue}>
      <TeamsEventsProvider>
        <CommandPaletteProvider>
          <DesktopPermissionsGate />
          <div className="workspace-shell">
            <WorkspaceSidebar onRecoveryReprovision={() => setRecoveryTriggered(true)} />
            <div className="workspace-main">
              <WorkspaceHeader onResetLayout={handleResetLayout} />
              <div className="workspace-content">
                <TrialBanner />
                <OutOfCreditsBanner />
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
            {chatReady && (
              <GetStarted
                derived={getStartedDerived}
                actions={{
                  chat: () => {
                    // Don't act on an empty id: if no real agent exists yet,
                    // bail rather than opening a chat panel for "".
                    const target = defaultIdRef.current;
                    if (!target) return;
                    // Record the focus intent BEFORE opening: when openChatPanel
                    // ADDS a new dock panel, its ChatInput hasn't mounted (or
                    // registered its window listener) yet — the CustomEvent below
                    // would be lost, so the freshly-mounted composer consumes
                    // this pending request instead. The already-mounted case is
                    // covered by the event; its listener also consumes the slot
                    // so the two paths can't double-fire.
                    requestFocusComposer(target);
                    openChatPanel(target);
                    window.dispatchEvent(
                      new CustomEvent("getstarted:focus-composer", { detail: { agentId: target } }),
                    );
                  },
                  task: () =>
                    openPanel("teams-inbox", {}, { pushUrl: true, oneShots: { newIssue: "onboarding" } }),
                  hire: () => openMarketplace({ view: "browse" }),
                  channel: () => {
                    const target = defaultIdRef.current;
                    if (!target) return;
                    openPanel(
                      "agent-inspector",
                      { agent: target, inspectorTab: "channels" },
                      { pushUrl: true },
                    );
                  },
                }}
              />
            )}
          </div>
        </CommandPaletteProvider>
      </TeamsEventsProvider>
    </InlineTeamsNavProvider>
  );
}
