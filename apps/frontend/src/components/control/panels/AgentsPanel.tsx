"use client";

import { useState } from "react";
import {
  Loader2,
  RefreshCw,
  Bot,
  Wrench,
  MessageSquare,
  Plus,
  Trash2,
} from "lucide-react";
import { AgentCreateForm } from "./AgentCreateForm";
import { AgentOverviewTab } from "./AgentOverviewTab";
import { AgentToolsTab } from "./AgentToolsTab";
import { AgentChannelsSection } from "./AgentChannelsSection";
import { useBilling } from "@/hooks/useBilling";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { AgentEntry, AgentsListResponse } from "./agents-types";

type AgentTab = "overview" | "tools" | "channels";

export function AgentsPanel() {
  const { data: rawData, error, isLoading, mutate } = useGatewayRpc<AgentsListResponse>("agents.list");
  const callRpc = useGatewayRpcMutation();
  // Channels require an active subscription (or trial). Pre-signup users
  // shouldn't see the Channels tab.
  const { isSubscribed } = useBilling();
  const channelsEnabled = isSubscribed;
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<AgentTab>("overview");
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const agents: AgentEntry[] = rawData?.agents ?? [];
  const defaultId = rawData?.defaultId;

  const handleAgentCreated = () => {
    mutate();
    setShowCreateForm(false);
  };

  const handleDelete = async (agentId: string) => {
    if (!confirm("Delete this agent?")) return;
    setDeleting(agentId);
    try {
      await callRpc("agents.delete", { agentId });
      if (selectedAgent === agentId) setSelectedAgent(null);
      mutate();
    } catch (err) {
      console.error("Failed to delete agent:", err);
    } finally {
      setDeleting(null);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-5 w-5 animate-spin text-[#8a8578]" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 space-y-3">
        <p className="text-sm text-[#dc2626]">{error.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw className="h-3.5 w-3.5 mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  const selected = agents.find((a) => a.id === selectedAgent);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#e0dbd0]">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold">Agents</h2>
          <span className="text-xs text-[#8a8578]">{agents.length}</span>
        </div>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="sm" onClick={() => setShowCreateForm(true)}>
            <Plus className="h-3.5 w-3.5 mr-1" /> New
          </Button>
          <Button variant="ghost" size="sm" onClick={() => mutate()}>
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {showCreateForm && (
        <div className="px-4 py-3 border-b border-[#e0dbd0]">
          <AgentCreateForm
            existingIds={agents.map((a) => a.id)}
            onCreated={handleAgentCreated}
            onCancel={() => setShowCreateForm(false)}
          />
        </div>
      )}

      <div className="flex flex-1 min-h-0">
        {/* Agent list */}
        <div className="w-48 border-r border-[#e0dbd0] overflow-y-auto">
          {agents.map((agent) => (
            <button
              key={agent.id}
              className={cn(
                "w-full flex items-center gap-2 px-3 py-2 text-left text-sm transition-colors group",
                selectedAgent === agent.id
                  ? "bg-[#e8f5e9] text-[#2d8a4e]"
                  : "hover:bg-[#f3efe6]",
              )}
              onClick={() => {
                setSelectedAgent(agent.id);
                setActiveTab("overview");
              }}
            >
              <Bot className="h-3.5 w-3.5 flex-shrink-0" />
              <span className="truncate flex-1">
                {agent.name || agent.id}
                {agent.id === defaultId && (
                  <span className="text-[10px] text-[#8a8578] ml-1">(default)</span>
                )}
              </span>
              {agent.id !== defaultId && (
                <button
                  className="opacity-0 group-hover:opacity-100 transition-opacity p-0.5 hover:text-[#dc2626]"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(agent.id);
                  }}
                  disabled={deleting === agent.id}
                  aria-label={`Delete ${agent.name || agent.id}`}
                >
                  {deleting === agent.id ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <Trash2 className="h-3 w-3" />
                  )}
                </button>
              )}
            </button>
          ))}
        </div>

        {/* Agent detail */}
        <div className="flex-1 overflow-y-auto px-4">
          {selected ? (
            <>
              <div className="flex items-center justify-between gap-2 border-b border-[#e0dbd0] py-2">
                <div className="flex items-center gap-1">
                  {(
                    [
                      { id: "overview", icon: Bot, label: "Overview" },
                      { id: "tools", icon: Wrench, label: "Tools" },
                      ...(channelsEnabled
                        ? [{ id: "channels", icon: MessageSquare, label: "Channels" } as const]
                        : []),
                    ] as const
                  ).map((tab) => (
                    <button
                      key={tab.id}
                      className={cn(
                        "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
                        activeTab === tab.id
                          ? "bg-[#e8f5e9] text-[#2d8a4e]"
                          : "text-[#8a8578] hover:text-[#1a1a1a]",
                      )}
                      onClick={() => setActiveTab(tab.id)}
                    >
                      <tab.icon className="h-3.5 w-3.5" />
                      {tab.label}
                    </button>
                  ))}
                </div>
              </div>

              {activeTab === "overview" && (
                <AgentOverviewTab agentId={selected.id} agent={selected} onAgentUpdated={() => mutate()} />
              )}
              {activeTab === "tools" && (
                <AgentToolsTab agentId={selected.id} />
              )}
              {activeTab === "channels" && channelsEnabled && (
                <AgentChannelsSection agentId={selected.id} />
              )}
            </>
          ) : (
            <div className="flex items-center justify-center py-12 text-sm text-[#8a8578]">
              Select an agent to view details
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
