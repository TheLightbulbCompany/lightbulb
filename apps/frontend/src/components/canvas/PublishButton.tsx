"use client";

// Publish the ready preview. The request returns at once; progress is the app list re-read every 5 s.
// usePublish owns the phase and the one interval; AppView calls it and passes the result to the header and the settings.
import { useEffect, useState } from "react";
import { useApps, type AppRow } from "@/hooks/useApps";
import "./canvas.css";

const POLL_MS = 5_000;
const GIVE_UP_MS = 10 * 60_000;

type Phase =
  | { k: "idle" }
  | { k: "publishing" | "slow"; from: number; noLive: boolean; fresh: boolean } // fresh: a poll ran after the POST, so a "failed" row is new
  | { k: "failed"; msg: string };

export function usePublish(app: AppRow) {
  const { publish, refresh } = useApps();
  const [phase, setPhase] = useState<Phase>({ k: "idle" });
  const waiting = phase.k === "publishing" || phase.k === "slow";
  const done = waiting && ((app.live_version ?? 0) > phase.from || (phase.noLive && !!app.live_url));
  const failed = waiting && phase.fresh && app.state === "failed";

  useEffect(() => {
    if (phase.k !== "publishing" || done || failed) return;
    const poll = setInterval(() => {
      refresh();
      setPhase((p) => (p.k === "publishing" && !p.fresh ? { ...p, fresh: true } : p));
    }, POLL_MS);
    const stop = setTimeout(() => setPhase((p) => (p.k === "publishing" ? { ...p, k: "slow" } : p)), GIVE_UP_MS);
    return () => { clearInterval(poll); clearTimeout(stop); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refresh is a fresh closure each render; phase+app is the trigger
  }, [phase.k, app.app_id, done, failed]);

  const start = async () => {
    setPhase({ k: "publishing", from: app.live_version ?? 0, noLive: !app.live_url, fresh: false });
    try {
      const res = await publish(app.app_id);
      if (res?.error || res?.state === "failed") setPhase({ k: "failed", msg: res.error?.message || "Publishing failed." });
    } catch (err) {
      setPhase({ k: "failed", msg: (err as Error)?.message || "Publishing failed." });
    }
  };
  return { phase, done, failed, start, refresh };
}

export type PublishCtl = ReturnType<typeof usePublish>;

export function PublishButton({ app, ctl }: { app: AppRow; ctl?: PublishCtl }) {
  return ctl ? <PublishView app={app} ctl={ctl} /> : <OwnPublish app={app} />;
}

function OwnPublish({ app }: { app: AppRow }) {
  return <PublishView app={app} ctl={usePublish(app)} />;
}

function PublishView({ app, ctl: { phase, done, failed, start, refresh } }: { app: AppRow; ctl: PublishCtl }) {
  const eligible = app.can_publish === true && app.state === "ready" && !!app.preview_url;
  const publishAgain = eligible && <button type="button" className="app-view__btn" onClick={start}>Publish</button>;
  if (done) return <span className="publish"><span role="status" className="canvas__notice">
    {app.live_url ? <>Live at <a href={app.live_url} target="_blank" rel="noopener noreferrer">{app.live_url}</a></> : "Live"}</span>{publishAgain}</span>;
  if (failed || phase.k === "failed") return <span className="publish">
    <span role="alert" className="canvas__notice">{phase.k === "failed" ? phase.msg : app.error?.message || "Publishing failed."}</span>
    <button type="button" className="app-view__btn" onClick={start}>Retry</button></span>;
  if (phase.k === "slow") return <span className="publish"><span role="status" className="canvas__notice">Still publishing — check back in a minute</span>
    <button type="button" className="app-view__btn" onClick={refresh}>Refresh</button></span>;
  if (phase.k === "publishing") return <span role="status" className="canvas__notice">Publishing…</span>;
  return publishAgain || null;
}
