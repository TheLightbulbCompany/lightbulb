"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { ChatLayout } from "@/components/chat/ChatLayout";
import { AgentChatWindow } from "@/components/chat/AgentChatWindow";
import { ControlPanelRouter } from "@/components/control/ControlPanelRouter";
import { GatewayProvider } from "@/hooks/useGateway";
import { MarketplaceDrawerProvider } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { MarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawer";
import { MarketplaceToast } from "@/components/marketplace/drawer/MarketplaceToast";
import { useUnifiedNav } from "@/hooks/useUnifiedNav";
import {
  InlineTeamsPanel,
  isTeamsPanelKey,
  TEAMS_PANEL_PREFIX,
} from "@/components/chat/InlineTeamsPanel";

export default function ChatPage() {
  // useSearchParams() forces dynamic rendering; wrap in Suspense so the
  // static-export step at build time doesn't error.
  return (
    <Suspense fallback={null}>
      <ChatPageInner />
    </Suspense>
  );
}

function ChatPageInner() {
  const searchParams = useSearchParams();
  const router = useRouter();
  // Single source of truth for view + panel is the URL. ?panel=credits is a
  // legacy deep link — the panel router remaps it to the Model & Credits
  // panel. The sidebar updates the URL via router.replace so in-app deep
  // links and clicks share the same code path.
  const panelParam = searchParams.get("panel");
  const viewParam = searchParams.get("view");
  const detailId = searchParams.get("detailId");
  const unifiedNav = useUnifiedNav();
  // Under the unified nav there's no tab-switcher: the view is derived purely
  // from ?panel (a Company/System panel → control; no panel → chat). Flag-off
  // keeps the legacy ?view-aware derivation byte-for-byte (the else arm).
  const activeView: "chat" | "control" = unifiedNav
    ? (panelParam ? "control" : "chat")
    : (viewParam === "control" || (viewParam !== "chat" && panelParam) ? "control" : "chat");
  const activePanel: string = panelParam || "overview";

  const setActiveView = useCallback(
    (next: "chat" | "control") => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("view", next);
      router.replace(`/chat?${params.toString()}`, { scroll: false });
    },
    [router, searchParams],
  );
  const setActivePanel = useCallback(
    (next: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("panel", next);
      params.set("view", "control");
      // Plain panel nav shows an UNFILTERED view: drop any stale agent
      // filter (from "See her work") and detail id (from an Open → row) so
      // switching panels never carries another panel's filter/detail state.
      params.delete("agent");
      params.delete("detailId");
      router.replace(`/chat?${params.toString()}`, { scroll: false });
    },
    [router, searchParams],
  );
  // Drops ?panel (and the per-panel filter/detail state) so the view falls
  // back to chat. Used when selecting an agent under the unified nav — there's
  // no tab-switcher, so chat is shown by absence of ?panel. Flag-off still sets
  // ?view=chat to keep the legacy ?view-aware derivation pointing at chat.
  const clearPanel = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("panel");
    params.delete("agent");
    params.delete("detailId");
    if (!unifiedNav) params.set("view", "chat");
    router.replace(`/chat?${params.toString()}`, { scroll: false });
  }, [router, searchParams, unifiedNav]);
  // Opens a Teams detail view inline: mirrors setActivePanel but also records
  // the row id in ?detailId so InlineTeamsPanel can resolve the detail panel.
  const openDetail = useCallback(
    (panelKey: string, id: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("panel", `${TEAMS_PANEL_PREFIX}${panelKey}`);
      params.set("detailId", id);
      params.set("view", "control");
      router.replace(`/chat?${params.toString()}`, { scroll: false });
    },
    [router, searchParams],
  );
  // "See her work →": opens the inline Issues panel filtered to one agent.
  // Mirrors openDetail but records the agent id in ?agent (which IssuesPanel
  // reads + filters on) instead of a detail row id.
  const openAgentIssues = useCallback(
    (agentId: string) => {
      const params = new URLSearchParams(searchParams.toString());
      params.set("panel", `${TEAMS_PANEL_PREFIX}issues`);
      params.set("agent", agentId);
      params.set("view", "control");
      router.replace(`/chat?${params.toString()}`, { scroll: false });
    },
    [router, searchParams],
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
      params.set("view", "control");
      router.replace(`/chat?${params.toString()}`, { scroll: false });
    },
    [router, searchParams],
  );

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
          activeView={activeView}
          onViewChange={setActiveView}
          activePanel={activePanel}
          onPanelChange={setActivePanel}
          openAgentIssues={openAgentIssues}
          openAgentInspector={openAgentInspector}
          onClearPanel={clearPanel}
          unifiedNav={unifiedNav}
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
              {unifiedNav && isTeamsPanelKey(activePanel) ? (
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
