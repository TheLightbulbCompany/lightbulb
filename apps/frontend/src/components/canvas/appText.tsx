"use client";

// Canvas wording shared by the list, the view, publish and settings: display name, audience, status lines, failures.
import { useState, type ReactNode } from "react";
import type { AppRow } from "@/hooks/useApps";

/** "workshop-signup" -> "Workshop Signup"; the backend's display_name wins when it sends one. */
export function displayName(app: Pick<AppRow, "name" | "display_name">): string {
  if (app.display_name?.trim()) return app.display_name.trim();
  return app.name.split(/[-_\s]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" ") || app.name;
}

export const roomOf = (app: AppRow) => `#${app.room_name ?? "room"}`;

/** Where the builder posts: the app's own conversation, else (an older app not opened since) its room. */
export const postsIn = (app: AppRow) => (app.conversation_id || !app.room_id ? "its conversation" : roomOf(app));

/** Who can open the app, as a phrase ("people in #finance", "anyone with the link"). */
export function audience(app: Pick<AppRow, "open_to" | "room_name">): string {
  switch (app.open_to) {
    case "me": return "only its creator and workspace admins";
    case "room": return `people in #${app.room_name ?? "room"}`;
    case "workspace": return "everyone in the workspace";
    case "invited": return "people you invited";
    default: return "anyone with the link";
  }
}

/** "Live — ..." naming who can open it; only a public app gets the address (shown by LiveLink). */
export function liveLine(app: AppRow): string {
  if (app.open_to === "public") return app.live_url ? "Live — anyone with the link:" : "Live — anyone with the link";
  return app.open_to === "room" ? `Live — ${audience(app)} can open it` : `Live — ${audience(app)}`;
}

/** The one line for an app that stopped moving or is not ready yet; null when it is ready. */
export function statusLine(app: AppRow): string | null {
  if (app.stuck) return `Stopped at ${app.stuck.step}. Nothing has moved for 5 minutes.`;
  const room = postsIn(app);
  switch (app.state) {
    case "awaiting_approval": return `Waiting for an owner to approve${app.requested_by?.trim() ? ` ${app.requested_by.trim()}'s request` : ""}: the cards are in Activity, under Needs you.`;
    case "provisioning": return "Setting up its hosting and data, usually a few minutes.";
    case "requested": // reserved by `app start`: the builder is on its first version
    case "building": return app.live_version == null // a first build, even over the starter placeholder's preview
      ? `The agent is making the preview. It'll post in ${room} when it's ready.`
      : `Building the new version. It'll show here and in ${room} when it's ready.`; // a publish or a change: not "the preview"
    case "failed": return `Something went wrong. Ask the agent in ${room}.`;
    default: return null;
  }
}

/** The backend's refusal code (`{detail: {code}}`), for the refusals where "Try again" would be false. */
export const refusalCode = (err: unknown) => (err as { body?: { detail?: { code?: string } } })?.body?.detail?.code;

/** Plain words first; the server's own text only behind "Details". */
export function Problem({ children, raw, action }: { children: ReactNode; raw?: string | null; action?: ReactNode }) {
  return <span role="alert" className="canvas__notice">
    {children}{" "}{action}
    {raw ? <details><summary>Details</summary>{raw}</details> : null}
  </span>;
}

/** The live address with Copy link (and Visit); a failed copy says so. */
export function LiveLink({ url }: { url: string }) {
  const [msg, setMsg] = useState<string | null>(null);
  const copy = async () => {
    try { await navigator.clipboard.writeText(url); setMsg("Link copied."); }
    catch { setMsg("Couldn't copy the link. Select the address and copy it."); }
  };
  return <span className="publish">
    <a href={url} target="_blank" rel="noopener noreferrer">{url}</a>
    <button type="button" className="app-view__btn" onClick={copy}>Copy link</button>
    <a className="app-view__btn" href={url} target="_blank" rel="noopener noreferrer">Visit</a>
    {msg && <span role="status" className="canvas__notice">{msg}</span>}
  </span>;
}
