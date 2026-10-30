"use client";

// Publish the ready preview. The request returns at once; progress is the app list re-read every 5 s.
import { useEffect, useState } from "react";
import { useApps, type AppRow } from "@/hooks/useApps";
import "./canvas.css";

const POLL_MS = 5_000;
const GIVE_UP_MS = 10 * 60_000;

type Phase = { k: "idle" } | { k: "publishing" | "slow"; from: number } | { k: "failed"; msg: string };

export function PublishButton({ app }: { app: AppRow }) {
  const { publish, refresh } = useApps();
  const [phase, setPhase] = useState<Phase & { id?: string }>({ k: "idle" });
  const mine: Phase = phase.id === app.app_id ? phase : { k: "idle" }; // never show another app's progress
  const waiting = mine.k === "publishing" || mine.k === "slow";
  const from = waiting ? mine.from : 0;
  const done = waiting && (app.live_version ?? 0) > from;
  const failed = waiting && app.state === "failed";

  useEffect(() => {
    if (mine.k !== "publishing" || done || failed) return;
    const poll = setInterval(refresh, POLL_MS);
    const stop = setTimeout(() => setPhase({ k: "slow", from: mine.from, id: app.app_id }), GIVE_UP_MS);
    return () => { clearInterval(poll); clearTimeout(stop); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refresh is a fresh closure each render; phase+app is the trigger
  }, [mine.k, app.app_id, done, failed]);

  const start = async () => {
    const id = app.app_id;
    setPhase({ k: "publishing", from: app.live_version ?? 0, id });
    try {
      const res = await publish(id);
      if (res?.error || res?.state === "failed") setPhase({ k: "failed", msg: res.error?.message || "Publishing failed.", id });
    } catch (err) {
      setPhase({ k: "failed", msg: (err as Error)?.message || "Publishing failed.", id });
    }
  };

  if (done) return <span role="status" className="canvas__notice">Live at <a href={app.live_url ?? undefined} target="_blank" rel="noopener noreferrer">{app.live_url}</a></span>;
  if (failed) return <span className="publish"><span role="alert" className="canvas__notice">{app.error?.message || "Publishing failed."}</span>
    <button type="button" className="app-view__btn" onClick={start}>Retry</button></span>;
  if (mine.k === "failed") return <span className="publish"><span role="alert" className="canvas__notice">{mine.msg}</span>
    <button type="button" className="app-view__btn" onClick={start}>Retry</button></span>;
  if (mine.k === "slow") return <span className="publish"><span role="status" className="canvas__notice">Still publishing — check back in a minute</span>
    <button type="button" className="app-view__btn" onClick={refresh}>Refresh</button></span>;
  if (mine.k === "publishing") return <span role="status" className="canvas__notice">Publishing…</span>;
  if (app.can_publish !== true || app.state !== "ready" || !app.preview_url) return null;
  return <button type="button" className="app-view__btn" onClick={start}>Publish</button>;
}
