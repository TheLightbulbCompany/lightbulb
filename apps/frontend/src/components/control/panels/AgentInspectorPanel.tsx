"use client";

import { useMemo, useState } from "react";
import { Bot, ScrollText, Wrench, CalendarClock } from "lucide-react";
import { AgentOverviewTab } from "./AgentOverviewTab";
import { AgentInstructionsTab } from "./AgentInstructionsTab";
import { AgentToolsTab } from "./AgentToolsTab";
import { RoutinesPanel } from "@/components/teams/panels/RoutinesPanel";
import { AgentOrgControls } from "@/components/teams/agents/AgentOrgControls";
import { useAgents } from "@/hooks/useAgents";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { cn } from "@/lib/utils";
import type { CompanyAgent } from "@/components/teams/shared/types";
import type { AgentEntry } from "./agents-types";

type InspectorTab = "overview" | "instructions" | "tools" | "schedule";

// Module-level tab metadata (no inline component literals).
const TABS: { id: InspectorTab; icon: typeof Bot; label: string }[] = [
  { id: "overview", icon: Bot, label: "Overview" },
  { id: "instructions", icon: ScrollText, label: "Instructions" },
  { id: "tools", icon: Wrench, label: "Tools" },
  { id: "schedule", icon: CalendarClock, label: "Schedule" },
];

function isInspectorTab(v: string | null | undefined): v is InspectorTab {
  return v != null && TABS.some((t) => t.id === v);
}

export interface AgentInspectorPanelProps {
  /** The inspected agent id (from the roster's "Inspect agent"). null = none. */
  agentId?: string | null;
  /** Deep-link initial tab (Get Started "Connect a channel" CTA). null = default. */
  initialTab?: string | null;
}

/**
 * Per-agent inspector shell (increment 1 of the unified-nav migration).
 *
 * Takes the inspected agent id as a prop (each shell reads its own channel: the
 * ?agent URL param on mobile, dock params on desktop) and re-mounts the EXISTING
 * per-agent control surfaces (Overview / Instructions / Tools) as tabs — the same
 * components AgentsPanel mounts in its detail pane, wired with the same props.
 * No new write path: every save still flows through the tabs' existing RPCs.
 */
export function AgentInspectorPanel({
  agentId = null,
  initialTab = null,
}: AgentInspectorPanelProps = {}) {
  // Header name/emoji: read from agents.list exactly as AgentsPanel does (the
  // inspector only has an id from the URL, not the agent object).
  const { agents: gatewayAgents } = useAgents();
  const agent: AgentEntry | undefined = (gatewayAgents as AgentEntry[]).find(
    (a: AgentEntry) => a.id === agentId,
  );

  // Org controls (reports-to + lifecycle) need the Teams roster shape, which
  // carries reportsTo/seeded/status — agents.list above does not. useTeamsApi
  // needs only Clerk (no Teams provider), so mounting it here is fine. Graceful
  // degrade: when the read errors OR the inspected agent isn't in the roster,
  // render NO org section — a pause/reports-to write against a missing
  // Paperclip row would 404.
  const { read } = useTeamsApi();
  const { data: roster, error: rosterError } =
    read<{ agents: CompanyAgent[] }>("/agents");
  const rosterAgents = useMemo(() => roster?.agents ?? [], [roster]);
  const orgAgent = useMemo<CompanyAgent | undefined>(
    () =>
      rosterError ? undefined : rosterAgents.find((a) => a.id === agentId),
    [rosterError, rosterAgents, agentId],
  );

  // A deep link can preselect a tab. Seeded at mount; manual tab clicks own the
  // state thereafter. A new command with a different tab re-applies (render-time
  // prev-compare) so a re-delivered intent isn't ignored. Falls back to Overview
  // for a missing/unknown tab.
  const canApplyInitialTab = isInspectorTab(initialTab);
  const [activeTab, setActiveTab] = useState<InspectorTab>(() =>
    canApplyInitialTab ? (initialTab as InspectorTab) : "overview",
  );
  const [prevInitialTab, setPrevInitialTab] = useState(initialTab);
  if (initialTab !== prevInitialTab) {
    setPrevInitialTab(initialTab);
    if (canApplyInitialTab) setActiveTab(initialTab as InspectorTab);
  }

  if (!agentId) {
    return (
      <div className="flex items-center justify-center py-12 text-sm text-[var(--midnight-muted)]">
        Select an agent to inspect.
      </div>
    );
  }

  const emoji = agent?.identity?.emoji;
  const displayName = agent?.name || agentId;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-[var(--edge)]">
        {emoji ? <span className="text-base leading-none">{emoji}</span> : null}
        <h2 className="text-sm font-semibold truncate">{displayName}</h2>
      </div>

      <div
        className="flex items-center gap-1 border-b border-[var(--edge)] px-4 py-2"
        role="tablist"
        aria-label="Agent inspector sections"
      >
        {TABS.map((tab) => (
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
                ? "bg-[var(--cream)] text-[var(--midnight)]"
                : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]",
            )}
            onClick={() => setActiveTab(tab.id)}
          >
            <tab.icon size={16} />
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
          <>
            <AgentOverviewTab agentId={agentId} agent={agent} />
            {orgAgent ? (
              <section
                className="mt-6 border-t border-[var(--edge)] pt-6"
                aria-labelledby="inspector-org-heading"
              >
                <h3
                  id="inspector-org-heading"
                  className="label-caps text-[var(--midnight-muted)] mb-3"
                >
                  Organization
                </h3>
                <AgentOrgControls agent={orgAgent} agents={rosterAgents} />
              </section>
            ) : null}
          </>
        ) : null}
        {activeTab === "instructions" ? (
          <AgentInstructionsTab agentId={agentId} />
        ) : null}
        {activeTab === "tools" ? <AgentToolsTab agentId={agentId} /> : null}
        {activeTab === "schedule" ? (
          // RoutinesPanel needs no TeamsEventsProvider/CommandPaletteProvider:
          // useTeamsApi is Clerk-backed and useInlineTeamsNav is null-safe. It
          // DOES need MarketplaceDrawerProvider (its empty state's "Hire an
          // agent" exit), which app/chat/page.tsx mounts above every route
          // that reaches this inspector.
          // The agentId prop scopes the list to this agent's routines (no chip);
          // the panel's own error/empty states cover an unavailable Teams side.
          <RoutinesPanel agentId={agentId} />
        ) : null}
      </div>
    </div>
  );
}
