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
  // Single source of truth for view + panel is the URL. ?panel=credits
  // opens the credits control panel directly (used by OutOfCreditsBanner's
  // CTA). The sidebar updates the URL via router.replace so in-app deep
  // links and clicks share the same code path. Codex P1/P2 on PR #393.
  const panelParam = searchParams.get("panel");
  const viewParam = searchParams.get("view");
  const activeView: "chat" | "control" =
    viewParam === "control" || (viewParam !== "chat" && panelParam) ? "control" : "chat";
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
              <ControlPanelRouter panel={activePanel} onPanelChange={setActivePanel} />
            </div>
          )}
        </ChatLayout>
        {/* Drawer + toast must live INSIDE GatewayProvider — useHire() and
            PickAgentStep both call useAgents() which calls useGateway().
            See PR #567 review (Codex P1). */}
        <MarketplaceDrawer />
        <MarketplaceToast />
      </GatewayProvider>
    </MarketplaceDrawerProvider>
  );
}
