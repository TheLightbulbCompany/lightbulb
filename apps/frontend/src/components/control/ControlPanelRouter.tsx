"use client";

import { OverviewPanel } from "./panels/OverviewPanel";
import { SessionsPanel } from "./panels/SessionsPanel";
import { UsagePanel } from "./panels/UsagePanel";
import { AgentsPanel } from "./panels/AgentsPanel";
import { SkillsTab } from "./panels/SkillsTab";
import { LLMPanel } from "./panels/LLMPanel";
import { IntegrationsPanel } from "./panels/IntegrationsPanel";
import { ChannelsStatusPanel } from "./panels/ChannelsStatusPanel";
import { DreamsPanel } from "./panels/DreamsPanel";
import { SkillProposalsPanel } from "./panels/SkillProposalsPanel";
import { AgentInspectorPanel } from "./panels/AgentInspectorPanel";


interface ControlPanelRouterProps {
  panel: string;
  onPanelChange?: (panel: string) => void;
  // Params the shell feeds the panels, read from its own channel (URL on
  // mobile, dockview params on desktop): { agentId?, initialTab?, action? }.
  // Legacy keys (e.g. credits→llm) are resolved by the shell/registry before
  // reaching here.
  panelParams?: Record<string, string>;
  onConsumeOneShot?: (key: "action") => void;
}

// Propless panels — rendered as <Panel /> with no params. Panels that take
// params (agents, llm, agent-inspector) are special-cased in the body below.
const PANELS: Record<string, React.ComponentType> = {
  overview: OverviewPanel,
  sessions: SessionsPanel,
  channels: ChannelsStatusPanel,
  usage: UsagePanel,
  skills: SkillsTab,
  integrations: IntegrationsPanel,
  dreams: DreamsPanel,
  "skill-proposals": SkillProposalsPanel,
};

export function ControlPanelRouter({
  panel,
  onPanelChange,
  panelParams,
  onConsumeOneShot,
}: ControlPanelRouterProps) {
  // OverviewPanel is the only propless panel that takes the panel-switch
  // callback — its summary cards deep-link into other panels.
  if (panel === "overview") {
    return <OverviewPanel onPanelChange={onPanelChange} />;
  }

  // Param-taking panels — threaded from panelParams.
  if (panel === "agents") {
    return <AgentsPanel initialAgentId={panelParams?.agentId ?? null} />;
  }
  if (panel === "agent-inspector") {
    return (
      <AgentInspectorPanel
        agentId={panelParams?.agentId ?? null}
        initialTab={panelParams?.initialTab ?? null}
      />
    );
  }
  if (panel === "llm") {
    return (
      <LLMPanel
        initialAction={
          panelParams?.action === "change-provider" ? "change-provider" : null
        }
        onActionConsumed={() => onConsumeOneShot?.("action")}
      />
    );
  }

  const Panel = PANELS[panel] || PANELS.overview;
  return <Panel />;
}
