"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { Bot, Wrench, MessageSquare } from "lucide-react";
import { AgentOverviewTab } from "./AgentOverviewTab";
import { AgentToolsTab } from "./AgentToolsTab";
import { AgentChannelsSection } from "./AgentChannelsSection";
import { useBilling } from "@/hooks/useBilling";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { cn } from "@/lib/utils";
import type { AgentEntry, AgentsListResponse } from "./agents-types";

type InspectorTab = "overview" | "tools" | "channels";

// Module-level tab metadata (no inline component literals). The `channels`
// entry is filtered out at render time when the owner has no active
// subscription — same gate AgentsPanel applies.
const TABS: { id: InspectorTab; icon: typeof Bot; label: string }[] = [
  { id: "overview", icon: Bot, label: "Overview" },
  { id: "tools", icon: Wrench, label: "Tools" },
  { id: "channels", icon: MessageSquare, label: "Channels" },
];

/**
 * Per-agent inspector shell (increment 1 of the unified-nav migration).
 *
 * Reads the inspected agent id from `?agent` and re-mounts the EXISTING
 * per-agent control surfaces (Overview / Tools / Channels) as tabs — the same
 * components AgentsPanel mounts in its detail pane, wired with the same props.
 * No new write path: every save still flows through the tabs' existing RPCs.
 */
export function AgentInspectorPanel() {
  const searchParams = useSearchParams();
  const agentId = searchParams.get("agent");

  // Channels require an active subscription (or trial), mirroring AgentsPanel.
  const { isSubscribed } = useBilling();
  const channelsEnabled = isSubscribed;

  // Header name/emoji: read from agents.list exactly as AgentsPanel does (the
  // inspector only has an id from the URL, not the agent object).
  const { data } = useGatewayRpc<AgentsListResponse>("agents.list");
  const agent: AgentEntry | undefined = (data?.agents ?? []).find(
    (a) => a.id === agentId,
  );

  const [activeTab, setActiveTab] = useState<InspectorTab>("overview");

  if (!agentId) {
    return (
      <div className="flex items-center justify-center py-12 text-sm text-[#8a8578]">
        Select an agent to inspect.
      </div>
    );
  }

  const visibleTabs = TABS.filter(
    (tab) => tab.id !== "channels" || channelsEnabled,
  );
  const emoji = agent?.identity?.emoji;
  const displayName = agent?.name || agentId;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-[#e0dbd0]">
        {emoji ? <span className="text-base leading-none">{emoji}</span> : null}
        <h2 className="text-sm font-semibold truncate">{displayName}</h2>
      </div>

      <div
        className="flex items-center gap-1 border-b border-[#e0dbd0] px-4 py-2"
        role="tablist"
        aria-label="Agent inspector sections"
      >
        {visibleTabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={`inspector-tab-${tab.id}`}
            aria-selected={activeTab === tab.id}
            aria-controls="inspector-tabpanel"
            className={cn(
              "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
              activeTab === tab.id
                ? "bg-[var(--cream)] text-[var(--forest)]"
                : "text-[#8a8578] hover:text-[#1a1a1a]",
            )}
            onClick={() => setActiveTab(tab.id)}
          >
            <tab.icon className="h-3.5 w-3.5" />
            {tab.label}
          </button>
        ))}
      </div>

      <div
        className="flex-1 overflow-y-auto px-4"
        role="tabpanel"
        id="inspector-tabpanel"
        aria-labelledby={`inspector-tab-${activeTab}`}
      >
        {activeTab === "overview" ? (
          <AgentOverviewTab agentId={agentId} agent={agent} />
        ) : null}
        {activeTab === "tools" ? <AgentToolsTab agentId={agentId} /> : null}
        {activeTab === "channels" && channelsEnabled ? (
          <AgentChannelsSection agentId={agentId} />
        ) : null}
      </div>
    </div>
  );
}
