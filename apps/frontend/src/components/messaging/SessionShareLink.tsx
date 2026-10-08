"use client";

import { useEffect, useId, useState } from "react";
import { Link2 } from "lucide-react";
import { ApiError, type createMessagingClient, type LocalSession, type SessionLink, type SessionLinkAudience } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { copyText, copyTextWhenReady } from "./messageLinks";

type Client = Partial<Pick<ReturnType<typeof createMessagingClient>, "getLocalSessionLink" | "putLocalSessionLink" | "deleteLocalSessionLink">>;

/**
 * The session's one link (spec 2026-10-05-public-session-links §2.1): Share makes it and copies
 * it; the audience is the only setting and is named wherever the link is copied; Turn off (two
 * clicks) ends it everywhere. On a Claude session the link
 * is also what lets people pick it up or continue it.
 */
export function SessionShareLink({ session, client }: { session: LocalSession; client: Client }) {
  const [link, setLink] = useState<SessionLink | null>(null);
  // Share never sends an audience, so a read still in flight or one that failed cannot narrow an
  // "anyone" link; a failed read still says so.
  const [readFailed, setReadFailed] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [offArmed, setOffArmed] = useState(false);
  useEffect(() => {
    if (!offArmed) return;
    const timer = setTimeout(() => setOffArmed(false), 3000);
    return () => clearTimeout(timer);
  }, [offArmed]);
  const audienceId = useId();
  useEffect(() => {
    let alive = true;
    // A 404 is "no link yet": Share makes one.
    client.getLocalSessionLink?.(session.sessionId).then(
      l => { if (alive) setLink(l); },
      e => { if (alive && !(e instanceof ApiError && e.status === 404)) setReadFailed(true); },
    );
    return () => { alive = false; };
  }, [client, session.sessionId]);
  if (!client.putLocalSessionLink) return null;
  const save = async (audience?: SessionLinkAudience) => {
    setBusy(true); setStatus(null);
    try { const next = await client.putLocalSessionLink!(session.sessionId, audience); setLink(next); setReadFailed(false); return next; }
    catch { setStatus("Could not change the link. Try again."); return null; }
    finally { setBusy(false); }
  };
  // Said wherever the link is copied or its audience changes: a signed-out person cannot open a workspace link (ISOA-732).
  const who = (audience: SessionLinkAudience) => audience === "anyone" ? "Anyone with the link can open it." : "Only people in your workspace can open it.";
  const copied = (ok: boolean, audience: SessionLinkAudience) => `${ok ? "Link copied." : "Copy the link below."} ${who(audience)}`;
  const share = async () => {
    if (link?.url) { setStatus(copied(await copyText(link.url), link.audience)); return; }
    const made = save();
    const wrote = copyTextWhenReady(made.then(n => n?.url ?? ""));
    const next = await made;
    if (next?.url) setStatus(copied(await wrote, next.audience));
  };
  const setAudience = async (audience: SessionLinkAudience) => { if (await save(audience)) setStatus(who(audience)); };
  // Two clicks, like End session: the link stops working for everyone who has it. Prod 2026-10-08: an owner's own
  // client sent this DELETE 1.5 s after their audience change, and their signed-out open 5 s later was refused.
  // When the audience warning appeared, this button moved down onto the place of the Details footer's own "Copy link" (now "Copy link for teammates").
  const off = async () => {
    if (!offArmed) { setOffArmed(true); return; }
    setOffArmed(false); setBusy(true);
    try { await client.deleteLocalSessionLink?.(session.sessionId); setLink(null); setStatus("Link turned off. It no longer opens for anyone."); }
    catch { setStatus("Could not turn the link off. Try again."); }
    finally { setBusy(false); }
  };
  if (!link) return <div className="flex flex-col gap-2 text-sm">
    <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void share()}><Link2 aria-hidden="true" />Share</Button>
    <p className="session-setting-about">Makes a link only people in your workspace can open. You can change who can open it afterwards.</p>
    {readFailed && <p role="alert">Could not load this session&apos;s link. Share still works.</p>}
    {status && <p role="status">{status}</p>}
  </div>;
  return <div className="flex flex-col gap-2 text-sm">
    <label htmlFor={audienceId}>Who can open it</label>
    <select id={audienceId} className="room-input w-auto" value={link.audience} disabled={busy} onChange={e => void setAudience(e.target.value as SessionLinkAudience)}>
      <option value="workspace">Workspace only</option>
      <option value="anyone">Anyone with the link</option>
    </select>
    {link.audience === "workspace" && <p className="session-setting-about">Only people signed in to your workspace can open it. Anyone else is asked to sign in and then cannot open it.</p>}
    {link.audience === "anyone" && <p role="note" className="session-setting-warning">
      Anyone with the link sees this session&apos;s title, who runs it, the tool and its state{session.provider === "claude" ? ", and can continue it with a copy of the conversation in their Claude Code. Likely secrets are redacted from that copy; anything else in it is not." : "."}
    </p>}
    {link.audience === "anyone" && session.pickup?.privateRepo && <p className="session-setting-warning">This conversation includes code Claude read from your private repository.</p>}
    {session.provider === "claude" && <p className="session-setting-about">Teammates with the link can pick it up with your uncommitted changes, unredacted. Files your repository ignores are never uploaded. Kept until 7 days after the session ends.</p>}
    {/* The audience above, the link and its Copy together: what is copied is always next to who can open it. */}
    <div className="flex items-center gap-2">
      {link.url && <input aria-label="Session link" readOnly className="room-input min-w-0 flex-1 font-mono text-xs" value={link.url} onFocus={e => e.currentTarget.select()} />}
      <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void share()}><Link2 aria-hidden="true" />Copy link</Button>
    </div>
    {status && <p role="status">{status}</p>}
    <Button type="button" variant="ghost" size="sm" className="self-start" disabled={busy} onClick={() => void off()}>{offArmed ? "Click again to turn off" : "Turn off link"}</Button>
  </div>;
}
