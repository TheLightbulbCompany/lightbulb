"use client";

// The app a request made, on its card: name, where it stands, Open in Canvas (its preview and conversation), and the
// address of a public live app (any other opens through a pass in Canvas, as LiveLink).
// The link is the app's Creation row (`request` on the Canvas list); a request that made no app shows nothing.
import { useApps } from "@/hooks/useApps";
import { openPanel } from "@/components/workspace/workspaceController";
import { phaseOf } from "./AppList";
import { displayName } from "./appText";

export function RequestApp({ taskId }: { taskId: string }) {
  const { apps } = useApps();
  const app = apps.find((a) => a.request?.task_id === taskId);
  if (!app) return null;
  const url = app.open_to === "public" ? app.live_url : null;
  return <p className="relative z-10 mt-1 flex flex-wrap items-center gap-x-3 text-ui" data-request-app={app.app_id}>
    <span className="min-w-0 truncate"><span className="font-semibold">{displayName(app)}</span>{" · "}{phaseOf(app, false).label}</span>
    {url && <a className="underline" href={url} target="_blank" rel="noopener noreferrer">Open the app</a>}
    <button type="button" className="underline" onClick={(e) => { e.stopPropagation(); openPanel("canvas", { app: app.app_id }, { pushUrl: true }); }}>
      Open in Canvas
    </button>
  </p>;
}
