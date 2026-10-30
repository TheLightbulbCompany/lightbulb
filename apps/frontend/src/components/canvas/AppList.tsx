"use client";

// Canvas: every app in the workspace, one row each. Everyone sees every app; one the viewer
// cannot open is greyed with "ask in #room" (spec §4). Building apps are listed, not openable.
import type { AppRow } from "@/hooks/useApps";
import "./canvas.css";

const WHO: Record<string, string> = {
  workspace: "everyone here", invited: "people you invited", public: "anyone with the link",
};

function whoCanOpen(app: AppRow) {
  return app.open_to === "room" ? `#${app.room_name ?? "room"}` : WHO[app.open_to] ?? app.open_to;
}

function Row({ app, onOpen }: { app: AppRow; onOpen: (appId: string) => void }) {
  const building = app.state === "building";
  const blocked = !app.can_open;
  const last = app.last_opened_at ? `opened ${new Date(app.last_opened_at).toLocaleDateString()}` : "never opened";
  const body = <>
    <span className="app-item__name">{app.name}</span>
    <span className="app-item__line">{building ? "building" : `asked by ${app.created_by_name ?? "someone"} in #${app.room_name ?? "room"}`}</span>
    <span className="app-item__line">
      <span>{blocked ? `ask in #${app.room_name ?? "room"}` : whoCanOpen(app)}</span> · <span>{last}</span> · <span>${(app.spend_month_cents / 100).toFixed(2)} this month</span>
    </span>
  </>;
  if (building || blocked) {
    return <li className="app-item"><div className="app-item__row" aria-disabled="true">{body}</div></li>;
  }
  return <li className="app-item">
    <button type="button" className="app-item__row" onClick={() => onOpen(app.app_id)}>{body}</button>
  </li>;
}

export function AppList({ apps, loading, error, onOpen, onRetry }: {
  apps: AppRow[]; loading: boolean; error: Error | null;
  onOpen: (appId: string) => void; onRetry: () => void;
}) {
  return <div className="canvas">
    {error ? <p role="alert" className="canvas__notice">
      Couldn&apos;t load apps · <button type="button" className="underline" onClick={onRetry}>Retry</button>
    </p> : null}
    <section aria-labelledby="canvas-heading">
      <h2 id="canvas-heading" className="canvas__heading">Apps</h2>
      <ul className="app-list">{apps.map((app) => <Row key={app.app_id} app={app} onOpen={onOpen} />)}</ul>
    </section>
    {!loading && !error && apps.length === 0 &&
      <p className="canvas__notice">No apps yet. Ask an agent in a room to build one.</p>}
  </div>;
}
