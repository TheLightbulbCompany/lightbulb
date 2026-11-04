"use client";

// Canvas in the right pane: the workspace's apps. params.app selects one (deep link ?app=).
import { AppList } from "@/components/canvas/AppList";
import { AppView, AskAgent } from "@/components/canvas/AppView";
import { AppConversation } from "@/components/canvas/AppConversation";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { displayName, statusLine } from "@/components/canvas/appText";
import { useApps } from "@/hooks/useApps";
import { updatePanelParams } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

export function CanvasDockPanel({ id, params }: PanelProps) {
  const { apps, loading, error, refresh } = useApps();
  const canManage = useIsWorkspaceAdmin();
  const close = () => updatePanelParams(id, { app: undefined });
  const open = params.app ? apps.find((a) => a.app_id === params.app) : undefined;
  if (params.app) {
    // A failed app that still has a preview (a publish that did not land) gets the full view: the preview, Publish to retry, Settings.
    if (open && (["awaiting_approval", "provisioning", "building"].includes(open.state) || (open.state === "failed" && open.live_version == null && !open.preview_url))) {
      // Its builder talks while it is being set up, so its conversation is here too.
      return <div className="canvas canvas--pending">
        <h2 className="canvas__heading">{displayName(open)}</h2>
        <p className="canvas__notice">{statusLine(open)}</p>
        {open.stuck && (open.can_publish === true || canManage) && <AskAgent app={open} />}
        <button type="button" className="app-view__btn" onClick={close}>All apps</button>
        {open.can_open && <AppConversation app={open} />}
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
