"use client";

// One app in the pane: a pass from POST /apps/{id}/open becomes the iframe src (never logged or stored).
import { useEffect, useRef, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useApps, type AppRow } from "@/hooks/useApps";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import "./canvas.css";

export function AppView({ app, onBack }: { app: AppRow; onBack(): void }) {
  const { open, refresh } = useApps();
  const isAdmin = useIsWorkspaceAdmin();
  const [pass, setPass] = useState<{ id: string; url: string } | null>(null);
  const [refusal, setRefusal] = useState<{ id: string; msg: string } | null>(null);
  const [tabError, setTabError] = useState<string | null>(null);
  const current = useRef(app.app_id);
  current.current = app.app_id;
  // State is tagged with its app so a render right after a switch never shows the previous app's pass.
  const url = pass?.id === app.app_id ? pass.url : null;
  const refused = refusal?.id === app.app_id ? refusal.msg : null;
  const [settingsOpen, setSettingsOpen] = useState(false); // Task 4b renders AppSettings from this

  useEffect(() => {
    let cancelled = false;
    setTabError(null);
    open(app.app_id).then(
      (u) => { if (!cancelled) { setPass({ id: app.app_id, url: u }); setRefusal(null); } },
      (err: unknown) => {
        if (cancelled) return;
        setRefusal({ id: app.app_id, msg: (err as Error)?.message || "This app can't be opened right now." });
        refresh(); // the server recomputes can_open, so the list greys the row
      },
    );
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open/refresh are fresh closures each render; the app id is the trigger
  }, [app.app_id]);

  const openInTab = async () => {
    const id = app.app_id;
    setTabError(null);
    try {
      const u = await open(id);
      window.open(u, "_blank", "noopener");
    } catch (err) {
      if (current.current !== id) return;
      setTabError((err as Error)?.message || "This app can't be opened right now.");
      refresh();
    }
  };

  return <div className="app-view">
    <header className="app-view__bar">
      <button type="button" className="app-view__btn" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />All apps</button>
      <h2 className="app-view__name">{app.name}</h2>
      {app.live_version != null && <span className="app-view__version">v{app.live_version}</span>}
      <button type="button" className="app-view__btn" onClick={openInTab}>Open in its own tab</button>
      {isAdmin && <button type="button" className="app-view__btn" aria-expanded={settingsOpen}
        onClick={() => setSettingsOpen((v) => !v)}>Settings</button>}
    </header>
    {tabError && <p role="alert" className="canvas__notice">{tabError}</p>}
    {refused ? <p role="alert" className="canvas__notice">{refused}</p>
      : url ? <iframe key={app.app_id} title={app.name} src={url} className="canvas__frame"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups" />
      : <p className="canvas__notice">Opening…</p>}
  </div>;
}
