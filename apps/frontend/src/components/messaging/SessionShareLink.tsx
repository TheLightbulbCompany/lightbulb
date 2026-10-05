"use client";

import { useEffect, useId, useState } from "react";
import { Link2 } from "lucide-react";
import type { createMessagingClient, LocalSession, SessionLink, SessionLinkAudience } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { copyText } from "./messageLinks";

type Client = Partial<Pick<ReturnType<typeof createMessagingClient>, "getLocalSessionLink" | "putLocalSessionLink" | "deleteLocalSessionLink">>;

/**
 * The session's one link (spec 2026-10-05-public-session-links §2.1): Share makes it and copies
 * it; the audience is the only setting; Turn off ends it everywhere. On a Claude session the link
 * is also what lets people pick it up or continue it.
 */
export function SessionShareLink({ session, client }: { session: LocalSession; client: Client }) {
  const [link, setLink] = useState<SessionLink | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const audienceId = useId();
  useEffect(() => {
    let alive = true;
    // A 404 is "no link yet": Share makes one.
    client.getLocalSessionLink?.(session.sessionId).then(l => { if (alive) setLink(l); }, () => {});
    return () => { alive = false; };
  }, [client, session.sessionId]);
  if (!client.putLocalSessionLink) return null;
  const save = async (audience: SessionLinkAudience) => {
    setBusy(true); setStatus(null);
    try { const next = await client.putLocalSessionLink!(session.sessionId, audience); setLink(next); return next; }
    catch { setStatus("Could not change the link. Try again."); return null; }
    finally { setBusy(false); }
  };
  const share = async () => {
    const next = link ?? await save("workspace");
    if (next?.url) setStatus(await copyText(next.url) ? "Link copied." : "Copy the link below.");
  };
  const off = async () => {
    setBusy(true);
    try { await client.deleteLocalSessionLink?.(session.sessionId); setLink(null); setStatus("Link turned off."); }
    catch { setStatus("Could not turn the link off. Try again."); }
    finally { setBusy(false); }
  };
  return <div className="flex flex-col gap-2 text-sm">
    <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void share()}><Link2 aria-hidden="true" />{link ? "Copy link" : "Share"}</Button>
    {link && <>
      <label htmlFor={audienceId}>Who can open it</label>
      <select id={audienceId} className="room-input w-auto" value={link.audience} disabled={busy} onChange={e => void save(e.target.value as SessionLinkAudience)}>
        <option value="workspace">Workspace only</option>
        <option value="anyone">Anyone with the link</option>
      </select>
      {link.audience === "anyone" && <p role="note" className="session-setting-warning">
        Anyone with the link can read this conversation and continue it in their Claude Code. Likely secrets are redacted for them; anything else in it is not.
      </p>}
      {link.audience === "anyone" && session.pickup?.privateRepo && <p className="session-setting-warning">This conversation includes code Claude read from your private repository.</p>}
      {session.provider === "claude" && <p className="session-setting-about">Teammates with the link can pick it up with your uncommitted changes, unredacted. Files your repository ignores are never uploaded. Kept until 7 days after the session ends.</p>}
      {link.url && <input aria-label="Session link" readOnly className="room-input font-mono text-xs" value={link.url} onFocus={e => e.currentTarget.select()} />}
      <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void off()}>Turn off link</Button>
    </>}
    {status && <p role="status">{status}</p>}
  </div>;
}
