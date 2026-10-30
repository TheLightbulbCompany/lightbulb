"use client";

// One app in the pane: a pass from POST /apps/{id}/open becomes the iframe src (never logged or stored).
import { useEffect, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { useApps, type AppRow } from "@/hooks/useApps";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import "./canvas.css";

export function AppView({ app, onBack }: { app: AppRow; onBack(): void }) {
  const { open, refresh } = useApps();
  const isAdmin = useIsWorkspaceAdmin();
  const [url, setUrl] = useState<string | null>(null);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false); // Task 4b renders AppSettings from this

  useEffect(() => {
    let cancelled = false;
    setUrl(null);
    setRefusal(null);
    open(app.app_id).then(
      (u) => { if (!cancelled) setUrl(u); },
      (err: unknown) => {
        if (cancelled) return;
        setRefusal((err as Error)?.message || "This app can't be opened right now.");
        refresh(); // the server recomputes can_open, so the list greys the row
      },
    );
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- open/refresh are fresh closures each render; the app id is the trigger
  }, [app.app_id]);

  const openInTab = async () => {
    try {
      window.open(await open(app.app_id), "_blank", "noopener");
    } catch (err) {
      setRefusal((err as Error)?.message || "This app can't be opened right now.");
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
    {refusal ? <p role="alert" className="canvas__notice">{refusal}</p>
      : url ? <iframe key={app.app_id} title={app.name} src={url} className="canvas__frame"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups" />
      : <p className="canvas__notice">Opening…</p>}
  </div>;
}
