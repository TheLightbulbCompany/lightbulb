"use client";

// Canvas: every app in the workspace, one row each. Everyone sees every app; one the viewer
// cannot open is greyed with "ask in #room" (spec §4). Building apps are listed, not openable.
import type { AppRow } from "@/hooks/useApps";
import { Problem, displayName, statusLine } from "./appText";
import "./canvas.css";

const WHO: Record<string, string> = {
  workspace: "everyone here", invited: "invited people", public: "anyone with the link",
};

function whoCanOpen(app: AppRow) {
  return app.open_to === "room" ? `#${app.room_name ?? "room"}` : WHO[app.open_to] ?? app.open_to;
}

function Row({ app, onOpen, canManage }: { app: AppRow; onOpen: (appId: string) => void; canManage: boolean }) {
  const building = app.state === "building";
  const status = statusLine(app);
  const blocked = !app.can_open;
  const last = app.last_opened_at ? `opened ${new Date(app.last_opened_at).toLocaleDateString()}` : "never opened";
  const body = <>
    <span className="app-item__name">{displayName(app)}</span>
    <span className="app-item__line">{status ?? `asked by ${app.created_by_name ?? "someone"} in #${app.room_name ?? "room"}`}</span>
    <span className="app-item__line">
      <span>{blocked ? `ask in #${app.room_name ?? "room"}` : whoCanOpen(app)}</span> · <span>{last}</span>{(app.spend_month_cents ?? 0) > 0 && <> · <span>${(app.spend_month_cents! / 100).toFixed(2)} spent this month</span></>}
    </span>
  </>;
  // An admin opens a blocked app too: the view shows the refusal and Settings, the only way to change who can open it.
  if (building || (blocked && !canManage)) {
    return <li className="app-item"><div className="app-item__row" role="group" aria-disabled="true">{body}</div></li>;
  }
  return <li className="app-item">
    <button type="button" className="app-item__row" onClick={() => onOpen(app.app_id)}>{body}</button>
  </li>;
}

export function AppList({ apps, loading, error, onOpen, onRetry, canManage = false }: {
  canManage?: boolean; apps: AppRow[]; loading: boolean; error: Error | null;
  onOpen: (appId: string) => void; onRetry: () => void;
}) {
  return <div className="canvas">
    {error ? <div className="canvas__notice"><Problem raw={error.message}
      action={<button type="button" className="underline" onClick={onRetry}>Try again</button>}>
      We couldn&apos;t load the apps.</Problem></div> : null}
    <section aria-labelledby="canvas-heading">
      <h2 id="canvas-heading" className="canvas__heading">Apps</h2>
      <ul className="app-list">{apps.map((app) => <Row key={app.app_id} app={app} onOpen={onOpen} canManage={canManage} />)}</ul>
    </section>
    {loading && apps.length === 0 && !error && <p className="canvas__notice">Loading apps…</p>}
    {!loading && !error && apps.length === 0 &&
      <p className="canvas__notice">No apps yet. In any room, ask an agent for one, like &quot;a sign-up page for our workshop&quot;, and it appears here.</p>}
  </div>;
}
