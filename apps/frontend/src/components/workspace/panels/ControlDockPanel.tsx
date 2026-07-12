"use client";

import type { IDockviewPanelProps } from "dockview-react";
import { ControlPanelRouter } from "@/components/control/ControlPanelRouter";
import { openPanel } from "../workspaceController";
import { useDockPanelParams } from "./useDockPanelParams";

interface ControlDockPanelParams {
  controlKey: string; // "overview" | "sessions" | "agents" | "agent-inspector" | …
  agentId?: string;
  initialTab?: string;
  action?: string; // one-shot (llm change-provider)
}

export function ControlDockPanel(props: IDockviewPanelProps<ControlDockPanelParams>): React.ReactElement {
  // One-shots (action) re-deliver via updateParameters on an already-open
  // panel — the hook subscribes to parameter changes (merging dockview's
  // PARTIAL payloads) so a repeated command re-fires without wiping controlKey.
  const params = useDockPanelParams(props);

  return (
    // workspace-panel-page (workspace.css): this wrapper IS the panel's
    // scroll container — it collapses editorial-page's viewport min-height
    // to the dock slot and lets descendants size against the slot (cqh).
    <div className="editorial-page workspace-panel-page h-full overflow-auto">
      <ControlPanelRouter
        panel={params.controlKey}
        // OverviewPanel's summary cards deep-link into other control panels via
        // onPanelChange — route those through the controller so they open as
        // dock panels (and integrate with the URL) instead of no-oping.
        onPanelChange={(next) => openPanel(next, {}, { pushUrl: true })}
        panelParams={{
          ...(params.agentId ? { agentId: params.agentId } : {}),
          ...(params.initialTab ? { initialTab: params.initialTab } : {}),
          ...(params.action ? { action: params.action } : {}),
        }}
        onConsumeOneShot={(k) => props.api.updateParameters({ [k]: undefined })}
      />
    </div>
  );
}
