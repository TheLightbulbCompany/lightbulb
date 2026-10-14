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
  // Params the shell feeds the panels from its dockview params:
  // { agentId?, initialTab? }.
  panelParams?: Record<string, string>;
}

// The control dock keeps only the agent detail views. Overview, Usage, Skills,
// Integrations, Connections and Model live on the Settings page; their URL keys
// redirect there (panelRegistry.settingsHrefFor) before a router sees them.
const AgentsPanel = dynamic(
  () => import("./panels/AgentsPanel").then((m) => m.AgentsPanel),
  { loading: PanelSkeleton },
);
const AgentInspectorPanel = dynamic(
  () => import("./panels/AgentInspectorPanel").then((m) => m.AgentInspectorPanel),
  { loading: PanelSkeleton },
);

export function ControlPanelRouter({ panel, panelParams }: ControlPanelRouterProps) {
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
  return null;
}
