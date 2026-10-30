"use client";

// Canvas in the right pane: the workspace's apps. params.app selects one (deep link ?app=).
import { AppList } from "@/components/canvas/AppList";
import { AppView } from "@/components/canvas/AppView";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useApps } from "@/hooks/useApps";
import { updatePanelParams } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

export function CanvasDockPanel({ id, params }: PanelProps) {
  const { apps, loading, error, refresh } = useApps();
  const canManage = useIsWorkspaceAdmin();
  const close = () => updatePanelParams(id, { app: undefined });
  const open = params.app ? apps.find((a) => a.app_id === params.app) : undefined;
  if (params.app) {
    if (open && ["awaiting_approval", "provisioning", "building"].includes(open.state)) {
      return <div className="canvas">
        <p className="canvas__notice">{open.name} · {open.state === "building" ? "building" : open.state === "provisioning" ? "setting up" : "waiting for approval"}</p>
        <button type="button" className="app-view__btn" onClick={close}>All apps</button>
      </div>;
    }
    if (open) return <AppView key={open.app_id} app={open} onBack={close} />;
    if (error) return <AppList apps={[]} loading={false} error={error} onRetry={refresh} onOpen={() => {}} />;
    return <div className="canvas">
      <p className="canvas__notice">{loading ? "Opening…" : "That app isn't in this workspace."}</p>
      {!loading && <button type="button" className="app-view__btn" onClick={close}>All apps</button>}
    </div>;
  }
  return <AppList apps={apps} loading={loading} error={error} onRetry={refresh} canManage={canManage}
    onOpen={(appId) => updatePanelParams(id, { app: appId })} />;
}
