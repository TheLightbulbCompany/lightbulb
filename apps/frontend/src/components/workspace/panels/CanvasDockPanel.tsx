"use client";

// Canvas in the right pane: the workspace's apps. params.app selects one (deep link ?app=).
import { ArrowLeft } from "lucide-react";
import { AppList } from "@/components/canvas/AppList";
import { useApps } from "@/hooks/useApps";
import { updatePanelParams } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

export function CanvasDockPanel({ id, params }: PanelProps) {
  const { apps, loading, error, refresh } = useApps();
  const close = () => updatePanelParams(id, { app: undefined });
  const open = params.app ? apps.find((a) => a.app_id === params.app) : undefined;
  if (params.app) { // Task 4 replaces this placeholder with AppView
    return <div className="canvas">
      <button type="button" className="idea-detail__back" onClick={close}><ArrowLeft size={16} aria-hidden="true" />All apps</button>
      <h2 className="canvas__heading">{open?.name ?? params.app}</h2>
    </div>;
  }
  return <AppList apps={apps} loading={loading} error={error} onRetry={refresh}
    onOpen={(appId) => updatePanelParams(id, { app: appId })} />;
}
