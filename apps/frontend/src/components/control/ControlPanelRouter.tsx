"use client";

import { useEffect } from "react";
import { OverviewPanel } from "./panels/OverviewPanel";
import { SessionsPanel } from "./panels/SessionsPanel";
import { UsagePanel } from "./panels/UsagePanel";
import { CronPanel } from "./panels/CronPanel";
import { AgentsPanel } from "./panels/AgentsPanel";
import { SkillsTab } from "./panels/SkillsTab";
import { LLMPanel } from "./panels/LLMPanel";
import { IntegrationsPanel } from "./panels/IntegrationsPanel";
import { ChannelsStatusPanel } from "./panels/ChannelsStatusPanel";
import { DreamsPanel } from "./panels/DreamsPanel";


interface ControlPanelRouterProps {
  panel: string;
  onPanelChange?: (panel: string) => void;
}

const PANELS: Record<string, React.ComponentType> = {
  overview: OverviewPanel,
  sessions: SessionsPanel,
  channels: ChannelsStatusPanel,
  usage: UsagePanel,
  cron: CronPanel,
  agents: AgentsPanel,
  skills: SkillsTab,
  llm: LLMPanel,
  integrations: IntegrationsPanel,
  dreams: DreamsPanel,
};

export function ControlPanelRouter({ panel, onPanelChange }: ControlPanelRouterProps) {
  // "credits" is a legacy key — the panel merged into Model & Credits.
  // Remap unconditionally (bookmarks, ?panel=credits deep links) and sync
  // the parent so URL + sidebar highlight match what actually rendered.
  const resolvedPanel = panel === "credits" ? "llm" : panel;

  useEffect(() => {
    if (panel === "credits") onPanelChange?.("llm");
  }, [panel, onPanelChange]);

  // OverviewPanel is the only panel that takes the panel-switch callback —
  // its summary cards deep-link into other panels. Every other panel takes
  // no props.
  if (resolvedPanel === "overview") {
    return <OverviewPanel onPanelChange={onPanelChange} />;
  }

  const Panel = PANELS[resolvedPanel] || PANELS.overview;
  return <Panel />;
}
