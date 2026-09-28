"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { useSearchParams } from "next/navigation";
import { ChatLayout } from "@/components/chat/ChatLayout";
import { LogoMark } from "@/components/Logo";
import { MessagingWorkspace } from "@/components/messaging/MessagingWorkspace";
import { useAgents } from "@/hooks/useAgents";
import { ControlPanelRouter } from "@/components/control/ControlPanelRouter";
import { LiveEventsProvider } from "@/hooks/useLiveEvents";
import { SharedLiveViewHost } from "@/components/workspace/panels/sharedLiveView";
import { MarketplaceDrawerProvider } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { MarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawer";
import { MarketplaceToast } from "@/components/marketplace/drawer/MarketplaceToast";
import { RunNotifications } from "@/components/notifications/RunNotifications";
import {
  InlineTeamsPanel,
  isTeamsPanelKey,
  TEAMS_PANEL_PREFIX,
} from "@/components/chat/InlineTeamsPanel";
import { preloadTeamsPanels } from "@/components/teams/TeamsPanelRouter";
import { MONEY_SECTIONS, resolvePanelAlias } from "@/components/workspace/panelRegistry";
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";
import { useIsDesktop } from "@/hooks/useIsDesktop";

// Matches WorkspaceShell's own onboarding-gate spinner, which is what the
// desktop branch renders on first mount anyway (useOnboardingGate starts at
// "loading"), so the chunk fetch reads as the same settle rather than a flash
// of nothing.
function WorkspaceShellLoading() {
  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", gap: 12, minHeight: "100vh" }}>
      <LogoMark className="brand-mark--hero" />
      <div style={{ color: "var(--midnight-muted)", fontSize: 14 }}>Loading…</div>
    </div>
  );
}

// The dock shell statically imports dockview (~356 KB raw / 82 KB gzip) — dead
// weight for every mobile visitor, who never renders this branch. useIsDesktop's
// server snapshot is false, so the desktop branch is absent from SSR and from
// the hydration pass too; ssr:false therefore costs nothing here and keeps the
// browser-only dock out of the server bundle.
// The mobile shell is deliberately NOT split to match: it IS the SSR/hydration
// branch, so splitting it would delay first paint for EVERY visitor, and its
// heavy children (MessagingWorkspace, ControlPanelRouter) are shared with the
// desktop dock panels anyway — there's little unique weight to reclaim.
const WorkspaceShell = dynamic(
  () => import("@/components/workspace/WorkspaceShell").then((m) => m.WorkspaceShell),
  { ssr: false, loading: WorkspaceShellLoading },
);

export default function ChatPage() {
  // useSearchParams() forces dynamic rendering; wrap in Suspense so the
  // static-export step at build time doesn't error. The fallback matches the
  // shell's cream background so any suspension reads as the page settling,
  // not a white flash.
  return (
    <Suspense fallback={<div className="h-dvh w-full" style={{ background: "var(--cream)" }} />}>
      <ChatPageInner />
    </Suspense>
  );
}

function ChatPageInner() {
  // Desktop gets the dockable-workspace shell; mobile keeps the classic
  // single-pane ChatLayout. useIsDesktop's server snapshot is false, so SSR
  // renders mobile and the desktop shell swaps in on hydration. Crossing the
  // breakpoint remounts the other shell — chat state survives via module
  // stores + _messageCache; the dock layout persists in localStorage.
  const isDesktop = useIsDesktop();
  return (
    <MarketplaceDrawerProvider>
      <LiveEventsProvider>
        {isDesktop ? <WorkspaceShell /> : <MobileChatShell />}
        {/* Drawer + toast must live INSIDE LiveEventsProvider — useHire() and
            PickAgentStep both call useAgents() which calls useLiveEvents(). */}
        <MarketplaceDrawer />
        <MarketplaceToast />
        {/* Each branch mounts its own RunNotifications variant; only ONE renders
            at a time. Desktop derives its watched keys from the dock snapshot
            (no props); mobile threads the selected agent + view/panel (mounted
            inside MobileChatShell, colocated with the state that feeds it).
            Both stay inside LiveEventsProvider. */}
        {isDesktop ? <RunNotifications mode="desktop" /> : null}
        {/* The page's one browser live-view connection, shared by the panel and previews. */}
        <SharedLiveViewHost />
      </LiveEventsProvider>
    </MarketplaceDrawerProvider>
  );
}

// The mobile (single-pane) shell: the classic ChatLayout + URL-driven control
// panel composition. Holds all of the page's param derivation, nav helpers,
// selection state, and the mode="mobile" RunNotifications mount. The desktop
// branch (WorkspaceShell) owns its own equivalents — none of this runs there.
function MobileChatShell() {
  const searchParams = useSearchParams();
  // Single source of truth for view + panel is the URL. Legacy deep links
  // (?panel=credits, ?panel=teams-approvals, …) are resolved to their live key
  // via resolvePanelAlias below. The view is derived purely from ?panel (a
  // Company/System panel → control; no panel → chat); there's no tab-switcher.
  const panelParam = searchParams.get("panel");
  const detailId = searchParams.get("detailId");
  const activeView: "chat" | "control" = panelParam && panelParam !== "chat" ? "control" : "chat";
  const roomId = searchParams.get("room");
  const messageId = searchParams.get("message");
  // Resolve retired panel keys (credits→llm, teams-approvals→teams-inbox, …)
  // through the shared registry so the panel renders under its live key.
  // Resolution happens at READ time only — the address bar is left exactly as
  // the sender wrote it. This shell must NOT canonicalize ?panel in place: on
  // a desktop viewport it is the branch that mounts during hydration
  // (useIsDesktop's server snapshot is false), so a rewrite here lands before
  // the dynamically-imported WorkspaceShell ever reads window.location.search
  // — and the dock's reconciler deliberately needs the RAW key to carry a
  // retired panel's meaning onto its successor (?panel=teams-org-chart → the
  // Agents chart view; see workspaceShellReconciler.ts and panelRegistry.ts).
  const activePanel: string = panelParam ? resolvePanelAlias(panelParam) : "overview";

  // Per-panel params derived once from the URL (mobile's channel) and threaded
  // down. Panels no longer read the URL themselves; they consume these props and
  // report one-shot consumption / durable clears back through the callbacks
  // below, which strip the matching URL param (replace-only).
  const agentParam = searchParams.get("agent");
  const pathParam = searchParams.get("path");
  const dayParam = searchParams.get("day");
  // Same synthesis as `tab` above, for Money and Inbox: ?panel=teams-spend and
  // ?panel=teams-activity carry no &section=, because the section IS the key.
  // Every absorbed key aliases to the page that absorbed it, so reading the
  // section off the RAW key here can never leak onto another panel. A
  // hand-written ?section= wins nothing extra — TeamsPanelRouter validates it
  // before either panel sees it.
  const rawTeamsKey = panelParam?.startsWith(TEAMS_PANEL_PREFIX)
    ? panelParam.slice(TEAMS_PANEL_PREFIX.length)
    : null;
  const absorbedSection =
    rawTeamsKey === "activity"
      ? "recently"
      : // Bookmarks predating the pill tabs still carry ?tab=approvals; that
        // band is now "Needs you".
        rawTeamsKey === "inbox" && searchParams.get("tab") === "approvals"
        ? "needs-you"
        : rawTeamsKey && MONEY_SECTIONS.has(rawTeamsKey)
          ? rawTeamsKey
          : null;
  const sectionParam = searchParams.get("section") ?? absorbedSection;
  // Tasks' own grouping menu (TasksList.GROUP_OPTIONS) still offers "goal"/
  // "project" as explicit ?groupBy= values — teams-goals/teams-projects are
  // their own panels now, not aliases onto Tasks, so there is no RAW-key
  // synthesis to do here any more.
  const groupByParam = searchParams.get("groupBy") ?? null;
  // And for Agents: ?panel=teams-org-chart carries no &view=, because the view
  // IS the key. It aliases to teams-agents, so reading it off the RAW key here
  // can never leak onto another panel.
  const viewParam = searchParams.get("view") ?? (rawTeamsKey === "org-chart" ? "chart" : null);
  const oneShots = {
    new: searchParams.get("new") ?? undefined,
    newIssue: searchParams.get("newIssue") ?? undefined,
    action: searchParams.get("action") ?? undefined,
    inspectorTab: searchParams.get("inspectorTab") ?? undefined,
  };
  const stripParam = useCallback(
    (key: string) => {
      const params = new URLSearchParams(searchParams.toString());
      if (params.has(key)) {
        params.delete(key);
        replaceSearchParams(params);
      }
    },
    [searchParams],
  );
  const updateDurableParam = useCallback(
    (key: string, value: string | null) => {
      const params = new URLSearchParams(searchParams.toString());
      if (value === null) params.delete(key);
      else params.set(key, value);
      replaceSearchParams(params);
    },
    [searchParams],
  );
  // A detail view whose row was just deleted/hidden swaps itself for the list.
  // Replace, never push — Back must not resurrect a deleted row.
  // Preserves the ?agent filter so returning to a filtered list restores the filter.
  const leaveToList = useCallback(
    (listPanelKey: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("panel", listPanelKey);
      params.delete("detailId");
      replaceSearchParams(params);
    },
    [searchParams],
  );

  // Navigational transitions commit through the History API (shellNav) so
  // each in-shell move (panel switch, list→detail) pushes a history entry the
  // browser Back button can step through — Back from a task detail returns to
  // the list, not the landing page — without the RSC round-trip a router.push
  // on this force-dynamic route would pay. Param-canonicalizing moves
  // (clearing the panel) use replace; only user-initiated navigation grows
  // the history stack.
  const setActivePanel = useCallback(
    (next: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("panel", next);
      // Plain panel nav shows an UNFILTERED view: drop any stale agent
      // filter (from "See her work"), detail id (from an Open → row), and
      // inbox tab deep link so switching panels never carries another
      // panel's filter/detail state.
      params.delete("agent");
      params.delete("detailId");
      params.delete("tab");
      // Inbox's band and Tasks' grouping are durable on desktop via
      // clearablePanelParamKeys; mobile has no such registry, which is exactly
      // why `tab` is deleted here. Without these two, a run toast's
      // ?section=needs-you survives a plain sidebar nav and Inbox silently
      // scrolls to a band nobody asked for.
      params.delete("section");
      params.delete("groupBy");
      params.delete("view");
      pushSearchParams(params);
    },
    [searchParams],
  );
  // Drops ?panel (and the per-panel filter/detail state) so the view falls
  // back to chat. Used when selecting an agent — there's no tab-switcher, so
  // chat is shown by absence of ?panel. This is canonicalization back to the
  // bare chat URL, so it replaces instead of pushing.
  const clearPanel = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("panel");
    params.delete("agent");
    params.delete("detailId");
    params.delete("room");
    params.delete("message");
    replaceSearchParams(params);
  }, [searchParams]);
  // Opens a Teams detail view inline: mirrors setActivePanel but also records
  // the row id in ?detailId so InlineTeamsPanel can resolve the detail panel.
  // Pushes so Back returns from the detail view to the list.
  const openDetail = useCallback(
    (panelKey: string, id: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("panel", `${TEAMS_PANEL_PREFIX}${panelKey}`);
      params.set("detailId", id);
      pushSearchParams(params);
    },
    [searchParams],
  );
  // "See her work →": opens the inline Issues panel filtered to one agent.
  // Mirrors openDetail but records the agent id in ?agent (which IssuesPanel
  // reads + filters on) instead of a detail row id.
  const openAgentIssues = useCallback(
    (agentId: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("panel", `${TEAMS_PANEL_PREFIX}issues`);
      params.set("agent", agentId);
      pushSearchParams(params);
    },
    [searchParams],
  );
  // "Inspect agent": opens the per-agent inspector shell (Overview / Tools /
  // Channels) for one agent. Mirrors openAgentIssues but targets the plain
  // ControlPanelRouter key `agent-inspector` (NOT a teams-prefixed panel — the
  // inspector re-mounts the existing control surfaces, not a Teams view) and
  // records the agent id in ?agent (which AgentInspectorPanel reads).
  const openAgentInspector = useCallback(
    (agentId: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("panel", "agent-inspector");
      params.set("agent", agentId);
      // A plain inspect always opens on Overview — drop any stale
      // ?inspectorTab deep-link (set only by the Get Started "Connect a
      // channel" CTA) so it never carries over to a later inspect.
      params.delete("inspectorTab");
      pushSearchParams(params);
    },
    [searchParams],
  );
  // Sidebar create pill → "New task": route to the Tasks panel with a
  // ?new=task intent that IssuesPanel reads to auto-open its create dialog.
  // Keeps task-create a first-class action reachable from anywhere in the
  // shell, while reusing the full dialog (and its agent/project pickers) in
  // its home panel rather than a degraded shell-level copy.
  const startNewTask = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.set("panel", `${TEAMS_PANEL_PREFIX}issues`);
    params.set("new", "task");
    params.delete("agent");
    params.delete("detailId");
    pushSearchParams(params);
  }, [searchParams]);

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

  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  // `selectedAgentId` stays null until the user taps a roster row. The default
  // agent is only a fallback: the provider opens its DM for a returning
  // workspace, while a fresh one lands in #general.
  const { defaultId } = useAgents();
  const [fileViewerOpen, setFileViewerOpen] = useState(false);
  const [activeFilePath, setActiveFilePath] = useState<string | null>(null);

  const handleOpenFile = useCallback((path: string) => {
    setActiveFilePath(path);
    setFileViewerOpen(true);
  }, []);

  const handleCloseFileViewer = useCallback(() => {
    setFileViewerOpen(false);
    setActiveFilePath(null);
  }, []);

  useEffect(() => {
    function handleSelectAgent(e: Event) {
      const { agentId, source } = (e as CustomEvent<{ agentId: string; source?: string }>).detail;
      setSelectedAgentId(source === "default" ? null : agentId);
    }

    window.addEventListener("selectAgent", handleSelectAgent);

    return () => {
      window.removeEventListener("selectAgent", handleSelectAgent);
    };
  }, []);

  return (
    <>
      <ChatLayout
        statusIndicator={(
          <RunNotifications
            mode="mobile"
            selectedAgentId={selectedAgentId ?? defaultId ?? null}
            activeView={activeView}
            activePanel={activePanel}
          />
        )}
        activePanel={activePanel}
        onPanelChange={setActivePanel}
        openAgentIssues={openAgentIssues}
        openAgentInspector={openAgentInspector}
        onClearPanel={clearPanel}
        onNewTask={startNewTask}
        fileViewerOpen={fileViewerOpen}
        activeFilePath={activeFilePath}
        onOpenFile={handleOpenFile}
        onCloseFileViewer={handleCloseFileViewer}
      >
        {/* Keep the room workspace mounted but hidden so its canonical state
            and draft survive in-shell panel switches. */}
        <div className={activeView === "chat" ? "flex flex-col h-full min-h-0" : "hidden"}>
          <MessagingWorkspace initialAgentId={selectedAgentId} defaultAgentId={defaultId} roomId={roomId} messageId={messageId} />
        </div>
        {activeView === "control" && (
          <div className="flex flex-col h-full min-h-0">
            {isTeamsPanelKey(activePanel) ? (
              <InlineTeamsPanel
                panel={activePanel}
                detailId={detailId}
                openDetail={openDetail}
                leaveToList={leaveToList}
                panelParams={{
                  ...(agentParam ? { agent: agentParam } : {}),
                  ...(groupByParam ? { groupBy: groupByParam } : {}),
                  ...(viewParam ? { view: viewParam } : {}),
                  ...(pathParam ? { path: pathParam } : {}),
                  ...(dayParam ? { day: dayParam } : {}),
                  ...(sectionParam ? { section: sectionParam } : {}),
                  ...(oneShots.new ? { new: oneShots.new } : {}),
                  ...(oneShots.newIssue ? { newIssue: oneShots.newIssue } : {}),
                }}
                onConsumeOneShot={stripParam}
                onUpdateDurableParam={updateDurableParam}
              />
            ) : (
              <ControlPanelRouter
                panel={activePanel}
                onPanelChange={setActivePanel}
                panelParams={{
                  ...(agentParam ? { agentId: agentParam } : {}),
                  ...(oneShots.inspectorTab ? { initialTab: oneShots.inspectorTab } : {}),
                  ...(oneShots.action ? { action: oneShots.action } : {}),
                }}
                onConsumeOneShot={stripParam}
              />
            )}
          </div>
        )}
      </ChatLayout>
    </>
  );
}
