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
import { Problem, displayName, refusalCode, roomOf, statusLine } from "./appText";
import "./canvas.css";

/** "Ask the agent to fix it": one message in the app's room, as the person. Remounts per failure, so a new one can ask again. */
export function AskAgent({ app, error }: { app: AppRow; error?: string }) {
  const { askAgent } = useApps();
  const [state, setState] = useState<"idle" | "asking" | "asked" | { status?: number; code?: string; raw: string }>("idle");
  const room = roomOf(app);
  if (state === "asked") return <p role="status" className="canvas__notice">Asked the agent in {room}. It&apos;ll post there.</p>;
  const ask = async () => {
    setState("asking");
    try { await askAgent(app.app_id, error); setState("asked"); }
    catch (err) { setState({ status: (err as { status?: number })?.status, code: refusalCode(err), raw: (err as Error)?.message ?? "" }); }
  };
  return <div className="canvas__notice publish">
    <button type="button" className="app-view__btn" disabled={state === "asking"} onClick={ask}>{state === "asking" ? "Asking…" : "Ask the agent to fix it"}</button>
    {typeof state === "object" && <Problem raw={state.raw}>{state.status === 403
      ? `You're not in ${room}, so this can't be posted as you. Join it, or ask someone in it.`
      : state.code === "no_agent" ? `There's no agent in ${room} to ask. Add one to the room.`
      : `We couldn't ask in ${room}. Try again.`}</Problem>}
  </div>;
}

/** "Put back the previous version", confirmed in place like Publish. */
function PutBack({ app, previous, onDone }: { app: AppRow; previous: NonNullable<AppRow["previous_version"]>; onDone(line: string): void }) {
  const { rollback } = useApps();
  const [state, setState] = useState<"idle" | "confirm" | "working" | { code?: string; raw: string }>("idle");
  const dn = displayName(app);
  const date = new Date(previous.at).toLocaleDateString(undefined, { dateStyle: "medium" });
  const putBack = async () => {
    setState("working");
    // The line lives in the view: going back to the first version removes this button with the refreshed row.
    try {
      const res = await rollback(app.app_id, previous.n);
      setState("idle");
      onDone(res?.rebuilding ? `${dn} is going back to the version live on ${date}. This takes a few minutes.` : `${dn} is back to the version live on ${date}.`);
    } catch (err) { setState({ code: refusalCode(err), raw: (err as Error)?.message ?? "" }); }
  };
  if (state === "working") return <span role="status" className="canvas__notice">Putting it back…</span>;
  if (state === "confirm") return <span className="publish" role="group" aria-label="Confirm putting back">
    <span className="canvas__notice">{dn} goes back to the version live on {date}. Its data stays as it is.
      {previous.rebuild && " Who can open it has changed since then, so it's rebuilt first: this takes a few minutes, and the current version stays up until it's done."}
      {app.publishing && " The publish in progress will be cancelled."}</span>
    <button type="button" className="app-view__btn app-view__btn--primary" onClick={putBack}>Put it back</button>
    <button type="button" className="app-view__btn" onClick={() => setState("idle")}>Cancel</button>
  </span>;
  return <span className="publish">
    {typeof state === "object" && <Problem raw={state.raw}>{state.code === "version_unavailable"
      ? `That version can't be put back any more, so ${dn} didn't change. Publish a new version instead.`
      : `${dn} didn't change. Try again.`}</Problem>}
    <button type="button" className="app-view__btn" onClick={() => setState("confirm")}>Put back the previous version</button>
  </span>;
}

export function AppView({ app, onBack }: { app: AppRow; onBack(): void }) {
  const { open, refresh } = useApps();
  const isAdmin = useIsWorkspaceAdmin();
  const [target, setTarget] = useState<{ id: string; t: "live" | "preview" }>({ id: app.app_id, t: "live" });
  // Nothing live yet: the preview is the only thing to show. Both: the person's pick holds.
  const t: "live" | "preview" = app.live_version == null ? (app.preview_url ? "preview" : "live") : target.id === app.app_id && app.preview_url ? target.t : "live";
  const key = `${app.app_id}:${t}`; // a pass is tagged by app + target, never reused across either
  const [pass, setPass] = useState<{ id: string; url: string } | null>(null);
  const [refusal, setRefusal] = useState<{ id: string; status?: number; raw: string } | null>(null);
  const [tabError, setTabError] = useState<{ raw: string } | null>(null);
  const [attempt, setAttempt] = useState(0);
  const dn = displayName(app);
  const room = roomOf(app);
  const canManage = app.can_publish === true || isAdmin;
  const whoName = app.open_to === "room" ? room : app.open_to === "invited" ? "invited people" : app.open_to === "public" ? "anyone with the link" : "people in this workspace";
  const current = useRef(app.app_id);
  current.current = app.app_id;
  // State is tagged with its app so a render right after a switch never shows the previous app's pass.
  const url = pass?.id === key ? pass.url : null;
  const refused = refusal?.id === key ? refusal : null;
  const publishCtl = usePublish(app);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [putBackLine, setPutBackLine] = useState<{ id: string; text: string } | null>(null);
  const lastLive = useRef(app.live_version ?? 0);
  // The line is about the version that was put back: a publish starting or a later version landing ends it.
  useEffect(() => {
    const rose = (app.live_version ?? 0) > lastLive.current;
    lastLive.current = app.live_version ?? 0;
    if (rose || publishCtl.phase.k !== "idle") setPutBackLine(null);
  }, [app.live_version, publishCtl.phase.k]);

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
  const { phase } = publishCtl;
  const publishing = (phase.k === "publishing" || phase.k === "slow") && !publishCtl.done && !publishCtl.failed;
  // A live app keeps serving through a failed publish: the row says "live", so it is named here.
  const liveAppFailed = app.state !== "failed" && app.publish_failed === true && phase.k === "idle";
  const askable = canManage && !publishing && (!!app.stuck || app.state === "failed" || app.publish_failed === true || phase.k === "failed");

  return <div className="app-view">
    <header className="app-view__bar">
      <button type="button" className="app-view__btn" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" />All apps</button>
      <h2 className="app-view__name">{dn}</h2>
      {hasBoth && <span className="app-view__seg" role="group" aria-label="View">
        {(["preview", "live"] as const).map((v) => <button key={v} type="button" className="app-view__btn" aria-pressed={t === v}
          onClick={() => setTarget({ id: app.app_id, t: v })}>{v === "live" ? "Live" : "Preview"}</button>)}
      </span>}
      <PublishButton app={app} ctl={publishCtl} />
      {canManage && app.previous_version && !publishing && <PutBack key={app.app_id} app={app} previous={app.previous_version}
        onDone={(text) => { publishCtl.reset(); setPutBackLine({ id: app.app_id, text }); setAttempt((n) => n + 1); }} />}
      <button type="button" className="app-view__btn" onClick={openInTab}>Open in its own tab</button>
      {canManage && <button type="button" className="app-view__btn" aria-expanded={settingsOpen}
        onClick={() => setSettingsOpen((v) => !v)}>Settings</button>}
    </header>
    {app.state === "failed"
      ? <div className="canvas__notice"><Problem raw={app.error?.message ?? null}>The last publish didn&apos;t land.{canManage ? " Press Publish to try again, or ask" : " Ask"} the agent in {room}.</Problem></div>
      : pending && <p className="canvas__notice">{pending}</p>}
    {liveAppFailed && <div className="canvas__notice"><Problem raw={app.error?.message ?? null}>The last version didn&apos;t go live. People still see the version before it.</Problem></div>}
    {putBackLine?.id === app.app_id && <p role="status" className="canvas__notice">{putBackLine.text}</p>}
    {askable && <AskAgent key={app.app_id} app={app} error={phase.k === "failed" ? phase.msg : undefined} />}
    {!app.stuck && t === "preview" && (app.agent_working
      ? <p className="canvas__notice">This is a first version. The agent is still working on it and will post in {room} when it&apos;s ready.</p>
      : <p className="canvas__notice">You&apos;re looking at the preview. Only you and {room} see it until it&apos;s published{canManage ? ": press Publish, or say “ship it” in the room" : ""}. The data here is examples; the live app starts empty.</p>)}
    {!canManage && <p className="canvas__notice">{app.open_to === "room" ? `Open to ${room}` : `Open to ${whoName}`}. Only {app.created_by_name ?? "the person who asked for it"} or a workspace admin can change who can open it.</p>}
    {settingsOpen && <AppSettings app={app} publishCtl={publishCtl} onClose={() => setSettingsOpen(false)} onRemoved={onBack} />}
    {tabError && <div className="canvas__notice"><Problem raw={tabError.raw}>{dn} didn&apos;t open. Try again in a moment.</Problem></div>}
    {refused ? <div className="canvas__notice">{refused.status === 403
      ? <Problem raw={refused.raw}>Only {whoName} can open {dn}. Ask in {room}.</Problem>
      : <Problem raw={refused.raw} action={retry}>{dn} didn&apos;t open. Try again in a moment.</Problem>}</div>
      : url ? <iframe key={`${key}:${attempt}`} title={dn} src={url} className="canvas__frame"
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups" />
      : <p className="canvas__notice">Opening…</p>}
  </div>;
}
