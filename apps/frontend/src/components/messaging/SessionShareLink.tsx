"use client";

import { useEffect, useId, useState } from "react";
import { Link2 } from "lucide-react";
import { ApiError, type createMessagingClient, type LocalSession, type SessionLink, type SessionLinkAudience } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { copyText, copyTextWhenReady } from "./messageLinks";

type Client = Partial<Pick<ReturnType<typeof createMessagingClient>, "getLocalSessionLink" | "putLocalSessionLink" | "setLocalSessionSharing" | "setLocalSessionPickup">>;
type Who = "me" | SessionLinkAudience;

const ABOUT: Record<Who, string> = {
  me: "Only you see this session. Nothing it does now is shown to anyone later.",
  workspace: "People signed in to your workspace can watch it in Lightbulb. Anyone else is asked to sign in and then cannot open it.",
  anyone: "Anyone with the link can watch this terminal live without signing in. They cannot type.",
};

/**
 * Who can see a session, in one place (owner ruling 2026-10-08, the Google Doc model): Only me,
 * My workspace, or Anyone with the link, and the one link to copy. Only me is the server's
 * `set_sharing(false)` and takes a second click: it cuts everyone off at once. A link only shows
 * the session; "Let people continue this session" is the owner's separate switch (pick-up).
 */
export function SessionShareLink({ session, client }: { session: LocalSession; client: Client }) {
  const [link, setLink] = useState<SessionLink | null>(null);
  // Copy link never sends an audience, so a read still in flight or one that failed cannot narrow
  // an "anyone" link; a failed read still says so.
  const [readFailed, setReadFailed] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [armed, setArmed] = useState(false);
  const [shared, setShared] = useState(session.shared !== false);
  useEffect(() => { setShared(session.shared !== false); }, [session.shared]);
  const [continues, setContinues] = useState(!!session.pickup?.allowed);
  useEffect(() => { setContinues(!!session.pickup?.allowed); }, [session.pickup?.allowed]);
  const whoId = useId();
  const continueId = useId();
  useEffect(() => {
    let alive = true;
    // A 404 is "no link yet": Copy link makes one.
    client.getLocalSessionLink?.(session.sessionId).then(
      l => { if (alive) setLink(l); },
      e => { if (alive && !(e instanceof ApiError && e.status === 404)) setReadFailed(true); },
    );
    return () => { alive = false; };
  }, [client, session.sessionId]);
  if (!client.putLocalSessionLink) return null;
  const who: Who = !shared ? "me" : link?.audience === "anyone" ? "anyone" : "workspace";
  const save = async (audience?: SessionLinkAudience) => {
    try { const next = await client.putLocalSessionLink!(session.sessionId, audience); setLink(next); setReadFailed(false); return next; }
    catch { setStatus("Could not change who can see this. Try again."); return null; }
  };
  const choose = async (next: Who) => {
    setStatus(null);
    if (next === "me") { setArmed(true); return; }
    setArmed(false); setBusy(true);
    try {
      if (!shared) {
        try { await client.setLocalSessionSharing!(session.sessionId, true); setShared(true); }
        catch { setStatus("Could not change who can see this. Try again."); return; }
      }
      // A workspace choice with no link, or a workspace link already, has nothing to write.
      if ((next === "anyone" || link?.audience === "anyone") && !(await save(next))) return;
      setStatus(ABOUT[next]);
    } finally { setBusy(false); }
  };
  // The second click (the select was the first): everyone watching is cut off at once.
  const onlyMe = async () => {
    setArmed(false); setBusy(true);
    try {
      await client.setLocalSessionSharing!(session.sessionId, false);
      setShared(false);
      setLink(l => l && { ...l, audience: "workspace" }); // the server narrows the link with it
      setStatus("Only you can see this session now.");
    } catch { setStatus("Could not stop sharing. Try again."); }
    finally { setBusy(false); }
  };
  const copy = async () => {
    setStatus(null);
    const said = (ok: boolean, audience: SessionLinkAudience) => `${ok ? "Link copied." : "Copy the link below."} ${audience === "anyone" ? "Anyone with the link can watch." : "Only people in your workspace can open it."}`;
    if (link?.url) { setStatus(said(await copyText(link.url), link.audience)); return; }
    setBusy(true);
    const made = save();
    const wrote = copyTextWhenReady(made.then(n => n?.url ?? ""));
    const next = await made;
    setBusy(false);
    if (next?.url) setStatus(said(await wrote, next.audience));
  };
  const setContinue = async (enabled: boolean) => {
    setBusy(true); setStatus(null);
    try { setContinues(!!(await client.setLocalSessionPickup!(session.sessionId, enabled)).pickup?.allowed); }
    catch { setStatus("Could not change that. Try again."); }
    finally { setBusy(false); }
  };
  const shown: Who = armed ? "me" : who;
  return <div className="flex flex-col gap-2 text-sm">
    <label htmlFor={whoId} className="session-setting-label">Who can see this</label>
    <select id={whoId} className="room-input w-auto" value={shown} disabled={busy} onChange={e => void choose(e.target.value as Who)}>
      {client.setLocalSessionSharing && <option value="me">Only me</option>}
      <option value="workspace">My workspace</option>
      <option value="anyone">Anyone with the link</option>
    </select>
    {armed
      ? <Button type="button" variant="outline" size="sm" className="self-start" disabled={busy} onClick={() => void onlyMe()}>Click again to stop sharing</Button>
      : shown === "anyone"
        ? <p role="note" className="session-setting-warning">{ABOUT.anyone} They see everything the terminal prints, secrets included.</p>
        : <p className="session-setting-about">{ABOUT[shown]}</p>}
    {readFailed && <p role="alert">Could not load this session&apos;s link. Copy link still works.</p>}
    {who !== "me" && !armed && <>
      {/* The audience above, the link and its Copy together: what is copied is always next to who can open it. */}
      <div className="flex items-center gap-2">
        {link?.url && <input aria-label="Session link" readOnly className="room-input min-w-0 flex-1 font-mono text-xs" value={link.url} onFocus={e => e.currentTarget.select()} />}
        <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void copy()}><Link2 aria-hidden="true" />Copy link</Button>
      </div>
      {session.provider === "claude" && client.setLocalSessionPickup && <div className="session-setting">
        <div className="session-setting-text">
          <label htmlFor={continueId}>Let people continue this session</label>
          <div id={`${continueId}-about`} className="session-setting-about">
            Off, the link only shows the terminal. On, people who can open it can continue it in their own Claude Code with a copy of the conversation: teammates with your uncommitted changes, anyone else with likely secrets redacted.
          </div>
          {continues && who === "anyone" && session.pickup?.privateRepo && <p className="session-setting-warning">This conversation includes code Claude read from your private repository.</p>}
        </div>
        <Switch id={continueId} checked={continues} disabled={busy} onCheckedChange={next => void setContinue(next)} aria-describedby={`${continueId}-about`} />
      </div>}
    </>}
    {status && <p role="status">{status}</p>}
  </div>;
}
