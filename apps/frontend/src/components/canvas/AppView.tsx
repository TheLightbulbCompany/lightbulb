"use client";

// One app in the pane: a pass from POST /apps/{id}/open becomes the iframe src (never logged or stored).
import { useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useApps, type AppRow } from "@/hooks/useApps";
import { AppSettings } from "./AppSettings";
import { PublishButton, usePublish } from "./PublishButton";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";
import { notifyError } from "@/lib/notify";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { Problem, displayName, roomOf, statusLine } from "./appText";
import "./canvas.css";

export function AppView({ app, onBack }: { app: AppRow; onBack(): void }) {
  const { open, refresh } = useApps();
  const isAdmin = useIsWorkspaceAdmin();
  const [target, setTarget] = useState<{ id: string; t: "live" | "preview" }>({ id: app.app_id, t: "live" });
  const t = target.id === app.app_id && app.live_version != null && app.preview_url ? target.t : "live";
  const key = `${app.app_id}:${t}`; // a pass is tagged by app + target, never reused across either
  const [pass, setPass] = useState<{ id: string; url: string } | null>(null);
  const [refusal, setRefusal] = useState<{ id: string; status?: number; raw: string } | null>(null);
  const [tabError, setTabError] = useState<{ raw: string } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const dn = displayName(app);
  const room = roomOf(app);
  const canManage = app.can_publish === true || isAdmin;
  const whoName = app.open_to === "room" ? room : app.open_to === "invited" ? "invited people" : "people in this workspace";
  const current = useRef(app.app_id);
  current.current = app.app_id;
  // State is tagged with its app so a render right after a switch never shows the previous app's pass.
  const url = pass?.id === key ? pass.url : null;
  const refused = refusal?.id === key ? refusal : null;
  const publishCtl = usePublish(app);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTabError(null);
    open(app.app_id, t).then(
      (u) => { if (!cancelled) { setPass({ id: key, url: u }); setRefusal(null); } },
      (err: unknown) => {
        if (cancelled) return;
        setRefusal({ id: key, status: (err as { status?: number })?.status, raw: (err as Error)?.message ?? "" });
        refresh(); // the server recomputes can_open, so the list greys the row
      },
    );
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open/refresh are fresh closures each render; app+target is the trigger
  }, [key, attempt]);

  const openInTab = async () => {
    const id = app.app_id;
    setTabError(null);
    // The tab opens synchronously in the click (Safari blocks a window.open after an await).
    const w = openPendingTab();
    if (!w) { notifyError("Your browser blocked the new tab — allow popups for this site and try again."); return; }
    try {
      navigatePendingTab(w, await open(id, t));
    } catch (err) {
      closePendingTab(w);
      if (current.current !== id) return;
      setTabError({ raw: (err as Error)?.message ?? "" });
      refresh();
    }
  };

  const hasBoth = app.live_version != null && !!app.preview_url;
  const pending = statusLine(app);
  const retry = <button type="button" className="app-view__btn" onClick={() => setAttempt((n) => n + 1)}>Try again</button>;

  return <div className="app-view">
    <header className="app-view__bar">
      <button type="button" className="app-view__btn" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />All apps</button>
      <h2 className="app-view__name">{dn}</h2>
      {hasBoth && <span className="app-view__seg" role="group" aria-label="Version">
        {(["preview", "live"] as const).map((v) => <button key={v} type="button" className="app-view__btn" aria-pressed={t === v}
          onClick={() => setTarget({ id: app.app_id, t: v })}>{v === "live" ? "Live" : "Preview"}</button>)}
      </span>}
      <PublishButton app={app} ctl={publishCtl} />
      <button type="button" className="app-view__btn" onClick={openInTab}>Open in its own tab</button>
      {canManage && <button type="button" className="app-view__btn" aria-expanded={settingsOpen}
        onClick={() => setSettingsOpen((v) => !v)}>Settings</button>}
    </header>
    {pending && <p className="canvas__notice">{pending}</p>}
    {app.live_version == null && <p className="canvas__notice">Preview: not live yet.</p>}
    {t === "preview" && <p className="canvas__notice">You&apos;re looking at the preview. Only you and {room} see it until it&apos;s published.</p>}
    {!canManage && <p className="canvas__notice">{app.open_to === "room" ? `Open to ${room}` : `Open to ${whoName}`}. Only {app.created_by_name ?? "the person who asked for it"} or a workspace admin can change who can open it.</p>}
    {settingsOpen && <AppSettings app={app} publishCtl={publishCtl} onClose={() => setSettingsOpen(false)} onRemoved={onBack} />}
    {tabError && <p className="canvas__notice"><Problem raw={tabError.raw}>{dn} didn&apos;t open. Try again in a moment.</Problem></p>}
    {refused ? <p className="canvas__notice">{refused.status === 403
      ? <Problem raw={refused.raw}>Only {whoName} can open {dn}. Ask in {room}.</Problem>
      : <Problem raw={refused.raw} action={retry}>{dn} didn&apos;t open. Try again in a moment.</Problem>}</p>
      : url ? <iframe key={key} title={dn} src={url} className="canvas__frame"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups" />
      : <p className="canvas__notice">Opening…</p>}
  </div>;
}
