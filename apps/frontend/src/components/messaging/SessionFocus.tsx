"use client";

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { ApiError, sessionTitle, sessionViewKey, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { LocalSessionPanel, SessionMenu } from "./LocalSessionPanel";
import { SessionToolIcon } from "./SessionToolIcon";
import type { SidebarSession } from "./roomSections";

export interface SessionFocusTarget { sessionId: string }

/**
 * Focus placement: a shared session lives in the workspace, not in a room
 * (owner ruling 2026-09-24), so it fills the middle of the Messages panel on
 * its own. The workspace session list is the one source of what is shared,
 * but it excludes ended sessions -- one missing from it (once the list has
 * settled) is fetched directly, so an ended session (or one opened before the
 * list has ever loaded) still opens with its real status. "No longer shared"
 * is reserved for a genuine 404.
 */
export function SessionFocus({ focus, entries, loading, hintRevision, client, resolveSessionOwner, onAccessLost, onClose }: {
  focus: SessionFocusTarget; entries: readonly SidebarSession[]; loading: boolean; hintRevision: number;
  client: SessionSource["client"]; resolveSessionOwner: (humanUserId: string) => { ownerName: string; own: boolean };
  onAccessLost: () => void; onClose: () => void;
}) {
  const entry = entries.find((candidate) => candidate.session.sessionId === focus.sessionId);
  const coldLoading = loading && entries.length === 0;
  const fetchedFor = useRef<string | null>(null);
  const [fallback, setFallback] = useState<{ sessionId: string; session: LocalSession | null; notFound: boolean } | null>(null);
  useEffect(() => {
    if (entry || coldLoading || fetchedFor.current === focus.sessionId) return;
    fetchedFor.current = focus.sessionId;
    let alive = true;
    void client.getLocalSession(focus.sessionId)
      .then((session) => { if (alive) setFallback({ sessionId: focus.sessionId, session, notFound: false }); })
      .catch((e) => { if (alive) setFallback({ sessionId: focus.sessionId, session: null, notFound: e instanceof ApiError && e.status === 404 }); });
    return () => { alive = false; };
  }, [entry, coldLoading, client, focus.sessionId]);
  const resolvedFallback = fallback?.sessionId === focus.sessionId ? fallback : null;
  const session = entry?.session ?? resolvedFallback?.session ?? null;
  const owner = entry ?? (session ? resolveSessionOwner(session.humanUserId) : null);
  return <section className="room-panel room-session-focus" aria-label="Shared session">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold room-wrap flex items-center gap-2">{session ? <><SessionToolIcon provider={session.provider} /><span className="min-w-0">{sessionTitle(session)}</span></> : "Shared session"}</h3>
        <div className="ml-auto flex items-center gap-1">
          {session && owner && <SessionMenu session={session} ownerName={owner.ownerName} own={owner.own} />}
          <Button variant="ghost" size="icon-sm" aria-label="Close session" onClick={onClose}><X aria-hidden="true" /></Button>
        </div>
      </header>
      {session && owner ? <LocalSessionPanel key={sessionViewKey(session)} session={session} ownerName={owner.ownerName} own={owner.own}
        client={client} revision={hintRevision} onAccessLost={onAccessLost} />
        : <p role="status" className="p-4 text-sm text-muted-foreground">{resolvedFallback?.notFound ? "This session is no longer shared." : "Loading session…"}</p>}
    </div>
  </section>;
}
