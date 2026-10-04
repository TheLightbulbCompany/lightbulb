"use client";

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";
import { ApiError, sessionTitle, sessionViewKey, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { LocalSessionPanel } from "./LocalSessionPanel";
import { SessionToolIcon } from "./SessionToolIcon";
import { SessionRenameButton } from "./SessionRenameButton";
import type { Person } from "./NativeSessionTranscript";
import type { SidebarSession } from "./roomSections";

/** `sessionId: null` is a new, not-yet-started session (NewSessionView), or with `all` the All sessions list. */
export interface SessionFocusTarget { sessionId: string | null; all?: boolean }

/**
 * Focus placement: a shared session lives in the workspace, not in a room
 * (owner ruling 2026-09-24), so it fills the middle of the Messages panel on
 * its own. The workspace session list is the one source of what is shared,
 * but it excludes ended sessions -- one missing from it (once the list has
 * settled) is fetched directly, so an ended session (or one opened before the
 * list has ever loaded) still opens with its real status. "No longer shared"
 * is reserved for a genuine 404.
 */
export function SessionFocus({ focus, entries, loading, hintRevision, client, resolveSessionOwner, onAccessLost, onClose, participants, onMessageOwner, onEvent }: {
  onEvent?: SessionSource["onEvent"];
  focus: { sessionId: string }; entries: readonly SidebarSession[]; loading: boolean; hintRevision: number;
  client: SessionSource["client"]; resolveSessionOwner: (humanUserId: string) => { ownerName: string; own: boolean };
  onAccessLost: () => void; onClose: () => void;
  /** Workspace humans and agents, for naming prompt authors and receipts. */
  participants?: readonly Person[];
  /** Opens the DM with the session's owner (someone else's session only). */
  onMessageOwner?: (humanUserId: string, ownerName: string) => void;
}) {
  // A Claude session moved to the background continues as a fork (`forkedFrom`): show that one,
  // but only in place of an ENDED origin -- a live origin can have a fork of its own.
  const exact = entries.find((candidate) => candidate.session.sessionId === focus.sessionId);
  const entry = (exact && exact.session.status !== "ended" ? exact : undefined)
    ?? entries.find((candidate) => candidate.session.forkedFrom === focus.sessionId)
    ?? exact;
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
  const messageOwner = session && owner && !owner.own && onMessageOwner ? () => onMessageOwner(session.humanUserId, owner.ownerName) : undefined;
  return <section className="room-panel room-session-focus" aria-label="Shared session">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold room-wrap flex items-center gap-2">{session ? <><SessionToolIcon provider={session.provider} /><span className="min-w-0">{sessionTitle(session)}</span></> : "Shared session"}</h3>
        {session && owner?.own && client.setLocalSessionDisplayTitle && <SessionRenameButton session={session} onRename={client.setLocalSessionDisplayTitle} icon />}
        <Button className="ml-auto" variant="ghost" size="icon-sm" aria-label="Close session" onClick={onClose}><X aria-hidden="true" /></Button>
      </header>
      {session && owner ? <LocalSessionPanel key={sessionViewKey(session)} session={session} ownerName={owner.ownerName} own={owner.own}
        client={client} revision={hintRevision} onAccessLost={onAccessLost} participants={participants} onMessageOwner={messageOwner} onEvent={onEvent} nameOf={id => resolveSessionOwner(id).ownerName} />
        : <p role="status" className="p-4 text-sm text-muted-foreground">{resolvedFallback?.notFound ? "This session is no longer shared." : "Loading session…"}</p>}
    </div>
  </section>;
}
