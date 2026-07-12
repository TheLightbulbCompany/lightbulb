"use client";

import type { IDockviewPanelProps } from "dockview-react";
import { useEffect, useState } from "react";
import { ControlPanelRouter } from "@/components/control/ControlPanelRouter";
import { openPanel } from "../workspaceController";

interface ControlDockPanelParams {
  controlKey: string; // "overview" | "sessions" | "agents" | "agent-inspector" | …
  agentId?: string;
  initialTab?: string;
  action?: string; // one-shot (llm change-provider)
}

export function ControlDockPanel(props: IDockviewPanelProps<ControlDockPanelParams>): React.ReactElement {
  // One-shots (action) re-deliver via updateParameters on an already-open
  // panel — subscribe to parameter changes so a repeated command re-fires.
  const [params, setParams] = useState(props.params);
  useEffect(() => {
    const d = props.api.onDidParametersChange((next) => setParams(next as ControlDockPanelParams));
    return () => d.dispose();
  }, [props.api]);

  return (
    <div className="editorial-page h-full overflow-auto">
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
