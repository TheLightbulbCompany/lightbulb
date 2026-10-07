"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { MessageSquare, X } from "lucide-react";
import { ApiError, sessionTitle, sessionViewKey, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { LocalSessionPanel } from "./LocalSessionPanel";
import { SessionChat } from "./SessionChat";
import { SessionToolIcon } from "./SessionToolIcon";
import { SessionRenameButton } from "./SessionRenameButton";
import type { Person } from "./NativeSessionTranscript";
import type { SidebarSession } from "./roomSections";

/** `sessionId: null` is a new, not-yet-started session (NewSessionView), or with `all` the All sessions list. */
export interface SessionFocusTarget { sessionId: string | null; all?: boolean; chat?: boolean }

/**
 * Focus placement: a shared session lives in the workspace, not in a room
 * (owner ruling 2026-09-24), so it fills the middle of the Messages panel on
 * its own. The workspace session list is the one source of what is shared,
 * but it excludes ended sessions -- one missing from it (once the list has
 * settled) is fetched directly, so an ended session (or one opened before the
 * list has ever loaded) still opens with its real status. "No longer shared"
 * is reserved for a genuine 404.
 */
export function SessionFocus({ focus, entries, loading, hintRevision, client, resolveSessionOwner, onAccessLost, onClose, participants, onMessageOwner, onEvent, chatOpen = false, onChatOpenChange, onChatRoom }: {
  onEvent?: SessionSource["onEvent"];
  focus: { sessionId: string }; entries: readonly SidebarSession[]; loading: boolean; hintRevision: number;
  client: SessionSource["client"]; resolveSessionOwner: (humanUserId: string) => { ownerName: string; own: boolean };
  onAccessLost: () => void; onClose: () => void;
  /** Workspace humans and agents, for naming prompt authors and receipts. */
  participants?: readonly Person[];
  /** Opens the DM with the session's owner (someone else's session only). */
  onMessageOwner?: (humanUserId: string, ownerName: string) => void;
  /** The session's chat column is open (the context owns it: the main room lends it the live subscription). */
  chatOpen?: boolean;
  onChatOpenChange?: (open: boolean) => void;
  onChatRoom?: (roomId: string | null) => void;
}) {
  // A Claude session moved to the background continues as a fork (`forkedFrom`): show that one,
  // but only in place of an ENDED origin -- a live origin can have a fork of its own.
  const exact = entries.find((candidate) => candidate.session.sessionId === focus.sessionId);
  const entry = (exact && exact.session.status !== "ended" ? exact : undefined)
    ?? entries.find((candidate) => candidate.session.forkedFrom === focus.sessionId)
    ?? exact;
  const coldLoading = loading && entries.length === 0;
  const hasListedSession = Boolean(entry);
  const [retryRevision, setRetryRevision] = useState(0);
  const [fallback, setFallback] = useState<{ sessionId: string; session: LocalSession | null; notFound: boolean; error: boolean } | null>(null);
  const [metadataRevision, setMetadataRevision] = useState(0);
  useEffect(() => {
    return onEvent?.(event => {
      if (event.type === "workspace.sessions.changed" && !event.payload.transcript) setMetadataRevision(v => v + 1);
    });
  }, [onEvent]);
  const fallbackHintRevision = onEvent ? metadataRevision : hintRevision;
  const revalidate = useRef<((hint: number, retry: number) => void) | null>(null);
  useEffect(() => {
    if (hasListedSession) return;
    let alive = true;
    let pending = false;
    let queued = false;
    let attempts = 0;
    let lastRequested: { hint: number; retry: number } | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Keep the current request and its retry budget through same-session hints/list refreshes.
    // Coalesce those hints into one revalidation after this bounded sequence settles.
    const fetchSession = async () => {
      attempts += 1;
      try {
        const session = await client.getLocalSession(focus.sessionId);
        if (alive) setFallback({ sessionId: focus.sessionId, session, notFound: false, error: false });
      } catch (e) {
        if (!alive) return;
        const notFound = e instanceof ApiError && e.status === 404;
        if (!notFound && attempts < 3) {
          timer = setTimeout(() => void fetchSession(), attempts * 1000);
          return;
        }
        setFallback(previous => ({ sessionId: focus.sessionId,
          session: !notFound && previous?.sessionId === focus.sessionId ? previous.session : null,
          notFound, error: !notFound }));
      }
      if (!alive) return;
      pending = false;
      if (queued) { queued = false; request(); }
    };
    const request = () => {
      if (pending) { queued = true; return; }
      pending = true;
      attempts = 0;
      void fetchSession();
    };
    revalidate.current = (hint, retry) => {
      // Ordinary list polls do not restart a settled 404 or exhausted sequence.
      if (lastRequested?.hint === hint && lastRequested.retry === retry) return;
      lastRequested = { hint, retry };
      request();
    };
    return () => { alive = false; clearTimeout(timer); revalidate.current = null; };
  }, [hasListedSession, client, focus.sessionId]);
  useEffect(() => {
    if (!hasListedSession && !coldLoading) revalidate.current?.(fallbackHintRevision, retryRevision);
  }, [hasListedSession, coldLoading, client, focus.sessionId, fallbackHintRevision, retryRevision]);
  const retryFallback = () => {
    setFallback(current => current?.session ? { ...current, error: false } : null);
    setRetryRevision(v => v + 1);
  };
  const resolvedFallback = fallback?.sessionId === focus.sessionId ? fallback : null;
  const session = entry?.session ?? resolvedFallback?.session ?? null;
  const owner = entry ?? (session ? resolveSessionOwner(session.humanUserId) : null);
  const messageOwner = session && owner && !owner.own && onMessageOwner ? () => onMessageOwner(session.humanUserId, owner.ownerName) : undefined;
  // Chat beside the terminal (PR #3807). Unread is the server's count from
  // the session list, which refetches when a chat message arrives (useWorkspaceSessions); nothing is
  // counted here. Reading the chat zeroes it until the list refreshes.
  // Opening refreshes the session list: a first open creates the seat, and the list must carry its
  // `chatRoomId` for the room's next message to count (useWorkspaceSessions filters on it).
  const openSessionChat = client.openSessionChat;
  const openChat = useMemo(() => openSessionChat && ((sessionId: string) =>
    openSessionChat(sessionId).then((room) => { onAccessLost(); return room; })), [openSessionChat, onAccessLost]);
  const [readThrough, setReadThrough] = useState<LocalSession | null>(null);
  const toggleChat = () => {
    if (chatOpen) setReadThrough(session);
    onChatOpenChange?.(!chatOpen);
  };
  const unread = chatOpen || readThrough === session ? 0 : session?.chatUnread ?? 0;
  const showChat = Boolean(session && openChat && onChatOpenChange);
  return <section className={`room-panel room-session-focus${showChat && chatOpen ? " session-chat-open" : ""}`} aria-label="Shared session">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold room-wrap flex items-center gap-2">{session ? <><SessionToolIcon provider={session.provider} /><span className="min-w-0">{sessionTitle(session)}</span></> : "Shared session"}</h3>
        {session && owner?.own && client.setLocalSessionDisplayTitle && <SessionRenameButton session={session} onRename={client.setLocalSessionDisplayTitle} icon />}
        {showChat && <Button className="ml-auto" variant="ghost" size="sm" aria-pressed={chatOpen}
          aria-label={unread ? `Chat, ${unread} unread` : "Chat"} onClick={toggleChat}>
          <MessageSquare aria-hidden="true" />Chat{unread > 0 && <span className="session-chat-unread" aria-hidden="true">{unread > 99 ? "99+" : unread}</span>}
        </Button>}
        <Button className={showChat ? undefined : "ml-auto"} variant="ghost" size="icon-sm" aria-label="Close session" onClick={onClose}><X aria-hidden="true" /></Button>
      </header>
      {!hasListedSession && session && resolvedFallback?.error && <div className="p-4 text-sm text-muted-foreground">
        <p role="status">Could not refresh this session.</p>
        <Button variant="ghost" onClick={retryFallback}>Try again</Button>
      </div>}
      {session && owner ? <LocalSessionPanel key={sessionViewKey(session)} session={session} ownerName={owner.ownerName} own={owner.own}
        client={client} revision={hintRevision} onAccessLost={onAccessLost} participants={participants} onMessageOwner={messageOwner} onEvent={onEvent} nameOf={id => resolveSessionOwner(id).ownerName} />
        : <div className="p-4 text-sm text-muted-foreground">
          <p role="status">{resolvedFallback?.notFound ? "This session is no longer shared." : resolvedFallback?.error ? "Could not load this session." : "Loading session…"}</p>
          {resolvedFallback?.error && <Button variant="ghost" onClick={retryFallback}>Try again</Button>}
        </div>}
    </div>
    {showChat && chatOpen && session && openChat && <SessionChat key={session.sessionId} sessionId={session.sessionId}
      sessionOwnerId={session.humanUserId} openChat={openChat} onClose={toggleChat} onRoom={onChatRoom} />}
  </section>;
}
