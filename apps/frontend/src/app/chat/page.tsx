"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ChatLayout } from "@/components/chat/ChatLayout";
import { AgentChatWindow } from "@/components/chat/AgentChatWindow";
import { ControlPanelRouter } from "@/components/control/ControlPanelRouter";
import { GatewayProvider } from "@/hooks/useGateway";
import { MarketplaceDrawerProvider } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { MarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawer";
import { MarketplaceToast } from "@/components/marketplace/drawer/MarketplaceToast";
import {
  InlineTeamsPanel,
  isTeamsPanelKey,
  TEAMS_PANEL_PREFIX,
} from "@/components/chat/InlineTeamsPanel";
import { preloadTeamsPanels } from "@/components/teams/TeamsPanelRouter";
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";

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
  const searchParams = useSearchParams();
  // Single source of truth for view + panel is the URL. ?panel=credits is a
  // legacy deep link — the panel router remaps it to the Model & Credits
  // panel. The view is derived purely from ?panel (a Company/System panel →
  // control; no panel → chat); there's no tab-switcher.
  const panelParam = searchParams.get("panel");
  const detailId = searchParams.get("detailId");
  const activeView: "chat" | "control" = panelParam ? "control" : "chat";
  const activePanel: string = panelParam || "overview";

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
      // filter (from "See her work") and detail id (from an Open → row) so
      // switching panels never carries another panel's filter/detail state.
      params.delete("agent");
      params.delete("detailId");
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

  // ?view is legacy: old deep links carried it alongside ?panel, but nothing
  // reads it (the view derives from ?panel presence). Strip it from inbound
  // URLs so copied links stay canonical.
  useEffect(() => {
    if (!searchParams.has("view")) return;
    const params = new URLSearchParams(searchParams.toString());
    params.delete("view");
    replaceSearchParams(params);
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
      const customEvent = e as CustomEvent<{ agentId: string }>;
      setSelectedAgentId(customEvent.detail.agentId);
    }

    window.addEventListener("selectAgent", handleSelectAgent);

    return () => {
      window.removeEventListener("selectAgent", handleSelectAgent);
    };
  }, []);

  return (
    <MarketplaceDrawerProvider>
      <GatewayProvider>
        <ChatLayout
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
          {/* Keep AgentChatWindow mounted but hidden so chat state
              (messages, scroll position, streaming) survives view switches */}
          <div className={activeView === "chat" ? "flex flex-col h-full min-h-0" : "hidden"}>
            <AgentChatWindow key={selectedAgentId} agentId={selectedAgentId} onOpenFile={handleOpenFile} />
          </div>
          {activeView === "control" && (
            <div className="flex flex-col h-full min-h-0">
              {isTeamsPanelKey(activePanel) ? (
                <InlineTeamsPanel
                  panel={activePanel}
                  detailId={detailId}
                  openDetail={openDetail}
                />
              ) : (
                <ControlPanelRouter panel={activePanel} onPanelChange={setActivePanel} />
              )}
            </div>
          )}
        </ChatLayout>
        {/* Drawer + toast must live INSIDE GatewayProvider — useHire() and
            PickAgentStep both call useAgents() which calls useGateway(). */}
        <MarketplaceDrawer />
        <MarketplaceToast />
      </GatewayProvider>
    </MarketplaceDrawerProvider>
  );
}
