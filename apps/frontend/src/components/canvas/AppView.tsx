"use client";

// One app in the pane: a pass from POST /apps/{id}/open becomes the iframe src (never logged or stored).
import { useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useApps, type AppRow } from "@/hooks/useApps";
import { AppSettings } from "./AppSettings";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";
import { notifyError } from "@/lib/notify";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import "./canvas.css";

export function AppView({ app, onBack }: { app: AppRow; onBack(): void }) {
  const { open, refresh } = useApps();
  const isAdmin = useIsWorkspaceAdmin();
  const [target, setTarget] = useState<{ id: string; t: "live" | "preview" }>({ id: app.app_id, t: "live" });
  const t = target.id === app.app_id && app.live_version != null && app.preview_url ? target.t : "live";
  const key = `${app.app_id}:${t}`; // a pass is tagged by app + target, never reused across either
  const [pass, setPass] = useState<{ id: string; url: string } | null>(null);
  const [refusal, setRefusal] = useState<{ id: string; msg: string } | null>(null);
  const [tabError, setTabError] = useState<string | null>(null);
  const current = useRef(app.app_id);
  current.current = app.app_id;
  // State is tagged with its app so a render right after a switch never shows the previous app's pass.
  const url = pass?.id === key ? pass.url : null;
  const refused = refusal?.id === key ? refusal.msg : null;
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTabError(null);
    open(app.app_id, t).then(
      (u) => { if (!cancelled) { setPass({ id: key, url: u }); setRefusal(null); } },
      (err: unknown) => {
        if (cancelled) return;
        setRefusal({ id: key, msg: (err as Error)?.message || "This app can't be opened right now." });
        refresh(); // the server recomputes can_open, so the list greys the row
      },
    );
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open/refresh are fresh closures each render; app+target is the trigger
  }, [key]);

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
      setTabError((err as Error)?.message || "This app can't be opened right now.");
      refresh();
    }
  };

  return <div className="app-view">
    <header className="app-view__bar">
      <button type="button" className="app-view__btn" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />All apps</button>
      <h2 className="app-view__name">{app.name}</h2>
      <span className="app-view__version">{app.live_version == null ? "preview" : `v${app.live_version}`}</span>
      {app.live_version != null && app.preview_url && <button type="button" className="app-view__btn" aria-pressed={t === "preview"}
        onClick={() => setTarget({ id: app.app_id, t: t === "preview" ? "live" : "preview" })}>{t === "preview" ? "Live" : "Preview"}</button>}
      <button type="button" className="app-view__btn" onClick={openInTab}>Open in its own tab</button>
      {isAdmin && <button type="button" className="app-view__btn" aria-expanded={settingsOpen}
        onClick={() => setSettingsOpen((v) => !v)}>Settings</button>}
    </header>
    {settingsOpen && <AppSettings app={app} onClose={() => setSettingsOpen(false)} onRemoved={onBack} />}
    {tabError && <p role="alert" className="canvas__notice">{tabError}</p>}
    {refused ? <p role="alert" className="canvas__notice">{refused}</p>
      : url ? <iframe key={key} title={app.name} src={url} className="canvas__frame"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups" />
      : <p className="canvas__notice">Opening…</p>}
  </div>;
}
