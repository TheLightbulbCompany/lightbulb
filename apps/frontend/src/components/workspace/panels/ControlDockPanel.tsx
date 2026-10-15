"use client";

import { ControlPanelRouter } from "@/components/control/ControlPanelRouter";
import type { PanelProps } from "../panelComponents";

interface ControlDockPanelParams {
  controlKey: string; // "agents" | "agent-inspector"
  agentId?: string;
  initialTab?: string;
}

export function ControlDockPanel(props: PanelProps): React.ReactElement {
  const params = props.params as unknown as ControlDockPanelParams;

  return (
    // workspace-panel-page (workspace.css): this wrapper IS the panel's
    // scroll container — it collapses editorial-page's viewport min-height
    // to the slot and lets descendants size against the slot (cqh).
    <div className="editorial-page workspace-panel-page h-full overflow-auto">
      <ControlPanelRouter
        panel={params.controlKey}
        panelParams={{
          ...(params.agentId ? { agentId: params.agentId } : {}),
          ...(params.initialTab ? { initialTab: params.initialTab } : {}),
        }}
      />
    </div>
  );
}
