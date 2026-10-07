"use client";

import dynamic from "next/dynamic";
import { Loader2 } from "lucide-react";

// Shown while a panel's code-split chunk loads. Without a loading fallback,
// next/dynamic renders nothing and a first open flashes a blank pane. This is
// the same centred spinner each control panel shows while its own gateway
// data loads, so the chunk fetch and the panel's first render read as one wait
// rather than two.
function PanelSkeleton() {
  return (
    <div className="flex-1 flex items-center justify-center" aria-busy="true">
      <Loader2 size={20} className="animate-spin text-[var(--midnight-muted)]" />
    </div>
  );
}

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
// Loaders are declared apart from their dynamic() wrappers so the whole
// code-split set reads as one block.
const PANEL_LOADERS: Record<string, () => Promise<React.ComponentType>> = {
  overview: () => import("./panels/OverviewPanel").then((m) => m.OverviewPanel),
  usage: () => import("./panels/UsagePanel").then((m) => m.UsagePanel),
  skills: () => import("./panels/SkillsTab").then((m) => m.SkillsTab),
  integrations: () => import("./panels/IntegrationsPanel").then((m) => m.IntegrationsPanel),
  connections: () => import("./panels/ConnectionsPanel").then((m) => m.ConnectionsPanel),
};

const PANELS: Record<string, React.ComponentType> = Object.fromEntries(
  Object.entries(PANEL_LOADERS).map(([key, loader]) => [
    key,
    dynamic(loader, { loading: PanelSkeleton }),
  ]),
);

// Panels that take params/callbacks (unlike the propless entries in PANELS)
// get their own prop-typed dynamic wrappers. Overview is reachable both ways —
// its wrapper points at the same chunk as the PANELS entry, so the fallback
// path and the callback path never load it twice.
const OverviewPanel = dynamic(
  () => import("./panels/OverviewPanel").then((m) => m.OverviewPanel),
  { loading: PanelSkeleton },
);
const AgentsPanel = dynamic(
  () => import("./panels/AgentsPanel").then((m) => m.AgentsPanel),
  { loading: PanelSkeleton },
);
const AgentInspectorPanel = dynamic(
  () => import("./panels/AgentInspectorPanel").then((m) => m.AgentInspectorPanel),
  { loading: PanelSkeleton },
);
const LLMPanel = dynamic(
  () => import("./panels/LLMPanel").then((m) => m.LLMPanel),
  { loading: PanelSkeleton },
);

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
