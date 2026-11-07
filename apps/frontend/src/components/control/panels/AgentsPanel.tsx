"use client";

import { useState } from "react";
import {
  Loader2,
  RefreshCw,
  Bot,
  Wrench,
  Plus,
  Trash2,
} from "lucide-react";
import { AgentCreateForm } from "./AgentCreateForm";
import { AgentOverviewTab } from "./AgentOverviewTab";
import { AgentToolsTab } from "./AgentToolsTab";
import { AgentEmailSection } from "./AgentEmailSection";
import { useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";
import { useAgents } from "@/hooks/useAgents";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { AgentEntry } from "./agents-types";
import { notifyApiError } from "@/lib/notify";

type AgentTab = "overview" | "tools";

export interface AgentsPanelProps {
  /**
   * Deep-link: selects this agent on first render (the Teams "Edit in control
   * panel" link). The shell reads its own channel (the ?agent URL param on
   * mobile) and threads it down. An unknown id falls back to the list.
   */
  initialAgentId?: string | null;
}

export function AgentsPanel({ initialAgentId = null }: AgentsPanelProps = {}) {
  // Roster via the shared hook, not a raw agents.list: this panel is the
  // direct analogue of mobile's control Agents tab and needs the same
  // empty-roster latch fix and become-reachable revalidation.
  const { agents: rosterAgents, defaultId: rosterDefaultId, error, isLoading, refresh: mutate } =
    useAgents();
  const callRpc = useRuntimeRpcMutation();
  const [selectedAgent, setSelectedAgent] = useState<string | null>(
    () => initialAgentId,
  );
  // Deep links can arrive on an ALREADY-mounted panel (the desktop dock keeps
  // panels alive and re-delivers params on focus) — re-apply on a changed
  // initialAgentId during render (React's "adjust state when a prop changes"
  // pattern, mirroring AgentInspectorPanel). A cleared param (null) resets to
  // the list, matching the plain-nav-drops-stale-selection invariant.
  const [prevInitialAgentId, setPrevInitialAgentId] = useState(initialAgentId);
  if (initialAgentId !== prevInitialAgentId) {
    setPrevInitialAgentId(initialAgentId);
    setSelectedAgent(initialAgentId);
  }
  const [activeTab, setActiveTab] = useState<AgentTab>("overview");
  // "+ New" opens the create form.
  const [createStep, setCreateStep] = useState<"build" | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);

  const agents = rosterAgents as AgentEntry[];
  const defaultId = rosterDefaultId;

  const handleAgentCreated = () => {
    mutate();
    setCreateStep(null);
  };

  const handleDelete = async (agentId: string) => {
    if (!confirm("Delete this agent?")) return;
    setDeleting(agentId);
    try {
      await callRpc("agents.delete", { agentId });
      if (selectedAgent === agentId) setSelectedAgent(null);
      mutate();
    } catch (err) {
      notifyApiError(err, "Couldn't delete the agent");
    } finally {
      setDeleting(null);
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 size={20} className="animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }

  // No warming suppression here, deliberately. Mobile can hide a wake behind
  // a skeleton because it reads the container-status contract; this panel has
  // no equivalent, and suppressing the card without a loading affordance
  // rendered the panel as a successfully-loaded EMPTY roster ("Agents 0" with
  // a live New button) over a cold-starting container — telling the user
  // their agents are gone and inviting a non-idempotent agents.create against
  // a box that cannot answer. Showing the error is the honest answer until
  // this panel can ask the status contract too.
  if (error) {
    return (
      <div className="p-4 space-y-3">
        <p className="text-sm text-err">{error.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw size={16} className="mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  const selected = agents.find((a) => a.id === selectedAgent);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--edge)]">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold">Agents</h2>
          <span className="text-xs text-[var(--midnight-muted)]">{agents.length}</span>
        </div>
        <div className="flex items-center gap-1">
          <Button variant="outline" size="sm" onClick={() => setCreateStep("build")}>
            <Plus size={16} className="mr-1" /> New
          </Button>
          <Button variant="ghost" size="sm" onClick={() => mutate()} aria-label="Refresh agents">
            <RefreshCw size={16} />
          </Button>
        </div>
      </div>

      <div className="border-b border-[var(--edge)]">
        <AgentEmailSection />
      </div>

      {createStep === "build" && (
        <div className="px-4 py-3 border-b border-[var(--edge)]">
          <AgentCreateForm
            // Names only — the id is the identity, never a name (see dialog).
            existingNames={agents
              .map((a) => a.name)
              .filter((n): n is string => !!n)}
            onCreated={handleAgentCreated}
            onCancel={() => setCreateStep(null)}
          />
        </div>
      )}

      <div className="flex flex-1 min-h-0">
        {/* Agent list */}
        <div className="w-48 border-r border-[var(--edge)] overflow-y-auto">
          {agents.map((agent) => (
            <button
              key={agent.id}
              className={cn(
                "w-full flex items-center gap-2 px-3 py-2 text-left text-sm transition-colors group",
                selectedAgent === agent.id
                  ? "bg-cream-deep text-[var(--midnight)]"
                  : "hover:bg-[var(--cream-deep)]",
              )}
              onClick={() => {
                setSelectedAgent(agent.id);
                setActiveTab("overview");
              }}
            >
              <Bot size={16} className="shrink-0" />
              <span className="truncate flex-1">
                {agent.name || agent.id}
                {agent.id === defaultId && (
                  <span className="text-2xs text-[var(--midnight-muted)] ml-1">(default)</span>
                )}
              </span>
              {agent.id !== defaultId && (
                <button
                  className="opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-visible:opacity-100 transition-opacity duration-(--dur-fast) ease-out p-0.5 hover:text-err"
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(agent.id);
                  }}
                  disabled={deleting === agent.id}
                  aria-label={`Delete ${agent.name || agent.id}`}
                >
                  {deleting === agent.id ? (
                    <Loader2 size={12} strokeWidth={1.75} className="animate-spin" />
                  ) : (
                    <Trash2 size={12} strokeWidth={1.75} />
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
              <div className="flex items-center justify-between gap-2 border-b border-[var(--edge)] py-2">
                <div className="flex items-center gap-1">
                  {(
                    [
                      { id: "overview", icon: Bot, label: "Overview" },
                      { id: "tools", icon: Wrench, label: "Tools" },
                    ] as const
                  ).map((tab) => (
                    <button
                      key={tab.id}
                      className={cn(
                        "flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition-colors",
                        activeTab === tab.id
                          ? "bg-cream-deep text-[var(--midnight)]"
                          : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]",
                      )}
                      onClick={() => setActiveTab(tab.id)}
                    >
                      <tab.icon size={16} />
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
            </>
          ) : (
            <div className="flex items-center justify-center py-12 text-sm text-[var(--midnight-muted)]">
              Select an agent to view details
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
