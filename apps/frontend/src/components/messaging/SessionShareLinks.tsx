"use client";

import { useCallback, useEffect, useId, useState } from "react";
import { Link2 } from "lucide-react";
import type { createMessagingClient, LocalSession, LocalSessionShareLink } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { copyText } from "./messageLinks";

type Client = Partial<Pick<ReturnType<typeof createMessagingClient>, "createLocalSessionShareLink" | "listLocalSessionShareLinks" | "revokeLocalSessionShareLink">>;

/** The token rides in the fragment, which browsers never send to a server or a referrer. */
export function sharedSessionLink(token: string, origin = window.location.origin): string {
  return `${origin}/chat/shared#${encodeURIComponent(token)}`;
}

const day = (iso: string) => new Date(iso).toLocaleDateString([], { month: "short", day: "numeric" });

/**
 * Owner-only read-only snapshot links. Workspace-only: a link opens for a
 * signed-in member of this workspace and nobody else. There is deliberately no
 * public variant (see local_sessions.py, "Share links").
 */
export function SessionShareLinks({ session, client }: { session: LocalSession; client: Client }) {
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState(7);
  const [links, setLinks] = useState<LocalSessionShareLink[]>([]);
  const [created, setCreated] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const expiryId = useId();
  const refresh = useCallback(async () => {
    if (!client.listLocalSessionShareLinks) return;
    try { setLinks((await client.listLocalSessionShareLinks(session.sessionId)).links); } catch { /* the list is a courtesy */ }
  }, [client, session.sessionId]);
  useEffect(() => { if (open) void refresh(); }, [open, refresh]);
  if (!client.createLocalSessionShareLink) return null;
  const create = async () => {
    setBusy(true); setStatus(null);
    try {
      const link = await client.createLocalSessionShareLink!(session.sessionId, days);
      const url = sharedSessionLink(link.token!);
      setCreated(url);
      setStatus(await copyText(url) ? "Link copied." : "Copy the link below.");
      await refresh();
    } catch { setStatus("Could not create a link."); } finally { setBusy(false); }
  };
  const revoke = async (linkId: string) => {
    try { await client.revokeLocalSessionShareLink?.(session.sessionId, linkId); setStatus("Link revoked."); await refresh(); }
    catch { setStatus("Could not revoke that link."); }
  };
  return <>
    <Button type="button" variant="outline" size="sm" onClick={() => { setCreated(null); setStatus(null); setOpen(true); }}><Link2 aria-hidden="true" />Share snapshot…</Button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto text-sm">
        <DialogHeader><DialogTitle>Share a read-only snapshot</DialogTitle>
          <DialogDescription>Anyone signed in to this workspace who has the link can read this transcript as it is now. Later output is not included.</DialogDescription></DialogHeader>
        <div role="note" className="rounded-md border border-warn p-3">
          Transcripts can hold code, file paths, command lines and whatever a tool printed. Likely credentials were redacted on your Mac,
          but that is a backstop, not a guarantee. Read it before you share it.
        </div>
        <div className="flex items-center gap-2">
          <label htmlFor={expiryId}>Expires after</label>
          <select id={expiryId} className="room-input w-auto" value={days} onChange={event => setDays(Number(event.target.value))}>
            <option value={1}>1 day</option><option value={7}>7 days</option><option value={30}>30 days</option>
          </select>
          <Button type="button" size="sm" disabled={busy} onClick={() => void create()}>Create link</Button>
        </div>
        {created && <input aria-label="Share link" readOnly className="room-input font-mono text-xs" value={created} onFocus={event => event.currentTarget.select()} />}
        {status && <p role="status">{status}</p>}
        {links.length > 0 && <section aria-label="Active links"><p className="font-medium">Active links</p>
          <ul className="mt-1 flex flex-col gap-1">{links.map(link => <li key={link.linkId} className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground">Created {day(link.createdAt)} · expires {day(link.expiresAt)}</span>
            <Button type="button" variant="ghost" size="sm" onClick={() => void revoke(link.linkId)}>Revoke</Button>
          </li>)}</ul></section>}
      </DialogContent>
    </Dialog>
  </>;
}
