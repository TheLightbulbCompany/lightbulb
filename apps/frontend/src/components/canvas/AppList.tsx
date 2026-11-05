"use client";

// Canvas: every app in the workspace. Each row says what the app is, where it stands (icon + word), the one
// thing to do next, and who asked. Apps that need the person sort first. Everyone sees every app; one the
// viewer cannot open is greyed with where to ask (spec §4).
import { useState } from "react";
import { ArrowRight, CircleAlert, CircleCheck, Hammer, Hourglass, Plug, Radio, ShieldQuestion } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import type { AppRow } from "@/hooks/useApps";
import { openPanel, openTaskPane } from "@/components/workspace/workspaceController";
import { Problem, displayName, roomOf } from "./appText";
import { StartBox } from "./StartBox";
import "./canvas.css";

const WHO: Record<string, string> = {
  me: "only its creator and admins", workspace: "everyone here", invited: "invited people", public: "anyone with the link",
};

const whoCanOpen = (app: AppRow) => (app.open_to === "room" ? `people in ${roomOf(app)}` : WHO[app.open_to] ?? app.open_to);

type Tone = "need" | "bad" | "run" | "ok" | "idle";
type Phase = { label: string; tone: Tone; Icon: LucideIcon; next: string; review?: boolean; setup?: boolean };

/** Where the app stands, in one word, and the one thing that happens or needs doing next. */
export function phaseOf(app: AppRow, canManage: boolean): Phase {
  // Only a person can unblock it (no Stripe services account): that, before anything else, and where to do it.
  if (app.setup_blocker && app.live_version == null) return { label: "Needs setup", tone: "need", Icon: Plug, next: app.setup_blocker.message, setup: canManage };
  // Waiting on an owner is not stuck: nothing is meant to move until someone approves.
  if (app.stuck && app.state !== "awaiting_approval") return { label: "Stopped", tone: "bad", Icon: CircleAlert, next: `Nothing has moved for 5 minutes at ${app.stuck.step}. Open it to ask the agent.` };
  switch (app.state) {
    case "failed": return { label: "Failed", tone: "bad", Icon: CircleAlert, next: "The last build didn't work. Open it to ask the agent." };
    case "awaiting_approval": return canManage
      ? { label: "Needs approval", tone: "need", Icon: ShieldQuestion, next: "Approve its hosting and data to start the preview.", review: true }
      : { label: "Needs approval", tone: "need", Icon: ShieldQuestion, next: "Waiting for an owner to approve its hosting and data." };
    case "provisioning": return { label: "Setting up", tone: "run", Icon: Hourglass, next: "Setting up hosting and data, usually a few minutes." };
    case "requested":
    case "building":
      if (app.publishing) return { label: "Publishing", tone: "run", Icon: Radio, next: "Publishing. It'll be live in a minute or two." };
      return app.live_version == null
        ? { label: "Building", tone: "run", Icon: Hammer, next: "The agent is making the first version." }
        : { label: "Updating", tone: "run", Icon: Hammer, next: "The agent is building a new version. The live app keeps working." };
    case "ready": return app.agent_working
      ? { label: "First version", tone: "run", Icon: Hammer, next: "Try it now; the agent is still improving it." }
      : { label: "Ready to try", tone: "ok", Icon: CircleCheck, next: "Try the preview, then publish it." };
    case "live":
      if (app.publish_failed) return { label: "Live", tone: "bad", Icon: CircleAlert, next: "The last update didn't go live. People still see the version before it." };
      return { label: "Live", tone: "ok", Icon: Radio, next: app.preview_newer ? "A newer version is ready to publish." : `Open to ${whoCanOpen(app)}.` };
    default: return { label: "Unknown", tone: "idle", Icon: CircleAlert, next: "" };
  }
}

const RANK: Record<Tone, number> = { need: 0, bad: 1, run: 2, ok: 3, idle: 4 };
const FILTERS = { all: "All", progress: "In progress", live: "Live" } as const;
type Filter = keyof typeof FILTERS;
const inFilter = (app: AppRow, f: Filter) => f === "all" || (f === "live" ? app.live_version != null : app.live_version == null);
const stamp = (app: AppRow) => app.updated_at ?? app.last_opened_at ?? "";

function ago(iso: string): string | null {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!iso || !Number.isFinite(s)) return null;
  if (s < 60) return "just now";
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (s < 3600) return rtf.format(-Math.round(s / 60), "minute");
  if (s < 86400) return rtf.format(-Math.round(s / 3600), "hour");
  return rtf.format(-Math.round(s / 86400), "day");
}

/** First and last word, numbers skipped: "Run 12 Jobs" → RJ, so two "Run …" apps don't share a tile. */
function initials(name: string): string {
  const words = name.split(/\s+/).filter((w) => /^[A-Za-z]/.test(w));
  const pick = words.length > 1 ? [words[0], words[words.length - 1]] : words;
  return pick.map((w) => w[0]).join("").toUpperCase() || name.slice(0, 2).toUpperCase();
}

function Row({ app, phase, onOpen, canManage }: { app: AppRow; phase: Phase; onOpen: (appId: string) => void; canManage: boolean }) {
  const dn = displayName(app);
  const blocked = !app.can_open;
  const asker = app.created_by_name ?? app.requested_by;
  const where = app.room_name ? `#${app.room_name}` : null;
  const spend = (app.spend_month_cents ?? 0) > 0 ? `$${(app.spend_month_cents! / 100).toFixed(2)} this month` : null;
  const next = blocked
    ? (app.room_id ? (app.room_name ? `Ask in #${app.room_name} to open it.` : "Ask whoever shared it to open it.") : "Not shared with you.")
    : phase.next;
  const meta = [asker ? `Asked by ${asker}${where ? ` in ${where}` : ""}` : where ? `From ${where}` : null, ago(stamp(app)), spend].filter(Boolean);
  const body = <>
    <span className="app-tile" data-running={phase.tone === "run" || undefined} aria-hidden="true">{initials(dn)}</span>
    <span className="app-item__text">
      <span className="app-item__top">
        <span className="app-item__name">{dn}</span>
        <span className="app-state" data-tone={phase.tone}><phase.Icon size={14} aria-hidden="true" />{phase.label}</span>
      </span>
      {next && <span className="app-item__next">{next}</span>}
      {meta.length > 0 && <span className="app-item__meta">{meta.join(" · ")}</span>}
    </span>
  </>;
  // An admin opens a blocked app too: the view shows the refusal and Settings, the only way to change who can open it.
  const main = blocked && !canManage
    ? <div className="app-item__main" role="group" aria-disabled="true" aria-label={dn}>{body}</div>
    : <button type="button" className="app-item__main" onClick={() => onOpen(app.app_id)}>{body}</button>;
  return <li className="app-item">
    {main}
    {app.request && <button type="button" className="app-item__act" onClick={() => openTaskPane(app.request!.task_id)}>
      {app.request.identifier ? `Request ${app.request.identifier}` : "Its request"}<ArrowRight size={14} aria-hidden="true" />
    </button>}
    {phase.setup && <a className="app-item__act" href="/settings/connections">Open Connections<ArrowRight size={14} aria-hidden="true" /></a>}
    {phase.review && !blocked && <button type="button" className="app-item__act" onClick={() => openPanel("activity", {}, { pushUrl: true })}>
      Review<ArrowRight size={14} aria-hidden="true" />
    </button>}
  </li>;
}

export function AppList({ apps, loading, error, onOpen, onRetry, onStart, canManage = false }: {
  canManage?: boolean; apps: AppRow[]; loading: boolean; error: Error | null;
  onOpen: (appId: string) => void; onRetry: () => void;
  onStart?: (request: string) => Promise<{ app_id: string }>;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const rows = apps
    .map((app) => ({ app, phase: phaseOf(app, canManage) }))
    .sort((a, b) => RANK[a.phase.tone] - RANK[b.phase.tone] || stamp(b.app).localeCompare(stamp(a.app)));
  const shown = rows.filter((r) => inFilter(r.app, filter));
  return <div className="canvas">
    {error ? <div className="canvas__notice"><Problem raw={error.message}
      action={<button type="button" className="underline" onClick={onRetry}>Try again</button>}>
      We couldn&apos;t load the apps.</Problem></div> : null}
    {onStart && !error && <StartBox onStart={onStart} onOpen={onOpen} first={!loading && apps.length === 0} />}
    {apps.length > 0 && <section aria-labelledby="canvas-heading">
      <header className="app-list__head">
        <h2 id="canvas-heading" className="app-list__title">Apps</h2>
        {apps.length > 0 && <div className="app-list__filters" role="group" aria-label="Show">
          {(Object.keys(FILTERS) as Filter[]).map((f) => <button key={f} type="button" aria-pressed={filter === f} onClick={() => setFilter(f)}>
            {FILTERS[f]}<span className="app-list__n">{rows.filter((r) => inFilter(r.app, f)).length}</span>
          </button>)}
        </div>}
      </header>
      <ul className="app-list">{shown.map(({ app, phase }) => <Row key={app.app_id} app={app} phase={phase} onOpen={onOpen} canManage={canManage} />)}</ul>
      {shown.length === 0 && <p className="canvas__notice">{filter === "live" ? "Nothing is live yet. Publish an app and it shows here." : "Every app here is live."}</p>}
    </section>}
    {loading && apps.length === 0 && !error && <p className="canvas__notice">Loading apps…</p>}
    {!loading && !error && apps.length === 0 && <div className="app-list__empty">
      <p className="app-list__empty-title">No apps yet</p>
      <p className="canvas__notice">{onStart ? "Describe one above, or ask an agent in any chat." : "Ask an agent in any chat for one, like \"a sign-up page for our workshop\"."} Each app is built in its own conversation and shows up here.</p>
    </div>}
  </div>;
}
