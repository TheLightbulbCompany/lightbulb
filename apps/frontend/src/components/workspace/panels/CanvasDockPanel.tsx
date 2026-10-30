"use client";

// Canvas in the right pane: the workspace's apps. params.app selects one (deep link ?app=).
import { AppList } from "@/components/canvas/AppList";
import { AppView } from "@/components/canvas/AppView";
import { useApps } from "@/hooks/useApps";
import { updatePanelParams } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

export function CanvasDockPanel({ id, params }: PanelProps) {
  const { apps, loading, error, refresh } = useApps();
  const close = () => updatePanelParams(id, { app: undefined });
  const open = params.app ? apps.find((a) => a.app_id === params.app) : undefined;
  if (params.app) {
    if (open) return <AppView app={open} onBack={close} />;
    return <div className="canvas">
      <p className="canvas__notice">{loading ? "Opening…" : "That app isn't in this workspace."}</p>
      {!loading && <button type="button" className="underline" onClick={close}>All apps</button>}
    </div>;
  }
  return <AppList apps={apps} loading={loading} error={error} onRetry={refresh}
    onOpen={(appId) => updatePanelParams(id, { app: appId })} />;
}
