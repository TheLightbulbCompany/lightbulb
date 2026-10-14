"use client";

import type { IDockviewPanelProps } from "dockview-react";
import { ControlPanelRouter } from "@/components/control/ControlPanelRouter";
import { useDockPanelParams } from "./useDockPanelParams";

interface ControlDockPanelParams {
  controlKey: string; // "agents" | "agent-inspector"
  agentId?: string;
  initialTab?: string;
}

export function ControlDockPanel(props: IDockviewPanelProps<ControlDockPanelParams>): React.ReactElement {
  // The hook subscribes to parameter changes (merging dockview's PARTIAL
  // payloads) so a re-targeted agent re-renders without wiping controlKey.
  const params = useDockPanelParams(props);

  return (
    // workspace-panel-page (workspace.css): this wrapper IS the panel's
    // scroll container — it collapses editorial-page's viewport min-height
    // to the dock slot and lets descendants size against the slot (cqh).
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
