"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { MessageSquare, X } from "lucide-react";
import { ApiError, sessionAgentName, sessionEndable, sessionTitle, sessionViewKey, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { LocalSessionPanel } from "./LocalSessionPanel";
import { preloadTerminal } from "./SessionTerminalView";
import { SessionChat } from "./SessionChat";
import { SessionToolIcon } from "./SessionToolIcon";
import { SessionRenameButton } from "./SessionRenameButton";
import { SessionRemoveButton } from "./SessionRemoveButton";
import type { Person } from "./NativeSessionTranscript";
import type { SidebarSession } from "./roomSections";

/** The session on screen; `seq` opens its conversation at that turn. */
export interface SessionFocusTarget { sessionId: string; chat?: boolean; seq?: number }

export type SessionChatLayout = "push" | "overlay" | "screen";
const CHAT_WIDTH = 344;
const TERMINAL_MIN_WIDTH = 420;
/**
 * Where the chat goes. Under 860px of window it takes the screen (the terminal stays mounted
 * underneath). Otherwise it is a column beside the terminal whenever the terminal keeps 420px; a
 * narrower panel gets an overlay that stops at the session's body, so the size strip above it and
 * the controls below it are never covered. The PANEL is measured: the rail and sidebar take the
 * rest of the window (prod 2026-10-07: a 984px window left a 709px panel and the overlay hid the controls).
 */
export function sessionChatLayout(panelWidth: number, viewportWidth: number): SessionChatLayout {
  if (viewportWidth < 860) return "screen";
  return panelWidth - CHAT_WIDTH >= TERMINAL_MIN_WIDTH ? "push" : "overlay";
}
/** The session's body: the live terminal's own box, or what stands in for it. */
const SESSION_BODY = ".session-terminal-scroll, .session-terminal-wait, .local-session-terminal, .session-state-panel, .local-session-history";

/**
 * A session its owner ended on purpose leaves every list (owner ruling 2026-10-08) but stays readable by id:
 * a panel open on it when it ends keeps the Ended state, the kept picture and Resume until it is closed.
 *
 * Focus placement: a shared session lives in the workspace, not in a room
 * (owner ruling 2026-09-24), so it fills the middle of the Messages panel on
 * its own. The workspace session list is the one source of what is shared,
 * but it excludes ended sessions -- one missing from it is fetched directly, at
 * once (not after the list settles), so a link opens on its own row and an ended
 * session still opens with its real status. "No longer shared"
 * is reserved for a genuine 404.
 */
export function SessionFocus({ focus, entries, hintRevision, client, resolveSessionOwner, onAccessLost, onClose, participants, onMessageOwner, onEvent, chatOpen = false, onChatOpenChange, onChatRoom, onRemove }: {
  onEvent?: SessionSource["onEvent"];
  focus: { sessionId: string; seq?: number }; entries: readonly Omit<SidebarSession, "owner">[]; hintRevision: number;
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
  /** The owner takes the session shown here off the list (a running one is ended first); the panel closes when that succeeded. */
  onRemove?: (sessionId: string) => Promise<unknown>;
}) {
  // A Claude session moved to the background continues as a fork (`forkedFrom`): show that one,
  // but only in place of an ENDED origin -- a live origin can have a fork of its own.
  const exact = entries.find((candidate) => candidate.session.sessionId === focus.sessionId);
  const entry = (exact && exact.session.status !== "ended" ? exact : undefined)
    ?? entries.find((candidate) => candidate.session.forkedFrom === focus.sessionId)
    ?? exact;
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
  // The row this panel last drew from the list. When it leaves the list (ended with no events yet, aged out,
  // a list refresh that raced the end) the panel keeps drawing it until the read by id answers: a panel left
  // with no session for that moment unmounted, and its terminal came back as a new one on "Connecting…"
  // (prod 2026-10-08).
  const [lastListed, setLastListed] = useState<LocalSession | null>(null);
  if (entry && entry.session !== lastListed) setLastListed(entry.session);
  // A read by id from before the session was listed is older than the list's row: never fall back to it.
  if (hasListedSession && fallback) setFallback(null);
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
  // Read by id at once, not after the list: the whole workspace list took 1.7-2.5 s and the terminal
  // waited for it (prod 2026-10-27). Whichever answers first draws the panel; the list's row then wins.
  useEffect(() => {
    if (!hasListedSession) revalidate.current?.(fallbackHintRevision, retryRevision);
  }, [hasListedSession, client, focus.sessionId, fallbackHintRevision, retryRevision]);
  // The terminal's code too, while the session is read: it was its own dynamic import after the row.
  useEffect(() => { void preloadTerminal().catch(() => { /* the view loads it again itself */ }); }, []);
  const retryFallback = () => {
    setFallback(current => current?.session ? { ...current, error: false } : null);
    setRetryRevision(v => v + 1);
  };
  const resolvedFallback = fallback?.sessionId === focus.sessionId ? fallback : null;
  const kept = lastListed && (lastListed.sessionId === focus.sessionId || lastListed.forkedFrom === focus.sessionId) ? lastListed : null;
  const session = entry?.session ?? resolvedFallback?.session ?? (resolvedFallback?.notFound ? null : kept);
  const owner = entry ?? (session ? resolveSessionOwner(session.humanUserId) : null);
  // A message another session sent arrives with that session's id as its author: name it as the terminal does ("Your Cursor", "Aden's Codex").
  const withSessions = useMemo(() => [...(participants ?? []), ...entries.map(({ session: s }) => {
    const who = resolveSessionOwner(s.humanUserId);
    return { kind: "agent" as const, id: s.sessionId, displayName: `${who.own ? "Your" : `${who.ownerName.split(" ")[0]}'s`} ${sessionAgentName(s.provider)}` };
  })], [participants, entries, resolveSessionOwner]);
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
  const chatShown = showChat && chatOpen;
  const panel = useRef<HTMLElement>(null);
  // Selection lifetime, not generation lifetime: End must not restore the run
  // again when LocalSessionPanel remounts for the next generation.
  // eslint-disable-next-line react-hooks/exhaustive-deps -- A new selection gets a fresh opening decision; a new generation retains it.
  const openedConversation = useMemo(() => ({ current: null as string | null }), [focus.sessionId, focus.seq]);
  const [chatLayout, setChatLayout] = useState<SessionChatLayout>("screen");
  useLayoutEffect(() => {
    const el = panel.current;
    if (!chatShown || !el) return;
    const watched = new Set<Element>();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => measure());
    const measure = () => {
      const box = el.getBoundingClientRect();
      const layout = sessionChatLayout(box.width, window.innerWidth);
      setChatLayout(layout);
      // The body is the one stretching row, so it resizes with every strip around it: watching it
      // (and the terminal view, which is there before its first frame) is watching them all.
      const bodies = [...el.querySelectorAll(`.room-session-main :is(${SESSION_BODY})`)];
      for (const target of [el, ...bodies, ...el.querySelectorAll(".room-session-main .session-terminal-view")]) {
        if (!watched.has(target)) { watched.add(target); observer?.observe(target); }
      }
      const body = bodies.map((candidate) => candidate.getBoundingClientRect()).find((rect) => rect.height > 0);
      el.style.setProperty("--session-chat-top", `${body ? Math.max(0, body.top - box.top) : 0}px`);
      el.style.setProperty("--session-chat-bottom", `${body ? Math.max(0, box.bottom - body.bottom) : 0}px`);
    };
    measure();
    window.addEventListener("resize", measure);
    return () => { observer?.disconnect(); window.removeEventListener("resize", measure); };
    // `session`: a status change swaps the body (live, ended, offline) without resizing anything watched.
  }, [chatShown, session]);
  return <section ref={panel} className={`room-panel room-session-focus${chatShown ? ` session-chat-open session-chat-${chatLayout}` : ""}`} aria-label="Shared session">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold room-wrap flex items-center gap-2">{session ? <><SessionToolIcon provider={session.provider} /><span className="min-w-0">{sessionTitle(session)}</span></> : "Shared session"}</h3>
        {session && owner?.own && client.setLocalSessionDisplayTitle && <SessionRenameButton session={session} onRename={client.setLocalSessionDisplayTitle} icon />}
        {session && owner?.own && onRemove && (session.status === "ended" || sessionEndable(session))
          && <SessionRemoveButton name={sessionTitle(session)} icon live={session.status !== "ended"}
          // The session shown may be the focused one's continuation (`forkedFrom`), under another id: close here, not by id.
          onRemove={async () => { await onRemove(session.sessionId); onClose(); }} />}
        {showChat && <Button className="ml-auto" variant="ghost" size="sm" aria-pressed={chatOpen}
          aria-label={unread ? `Chat, ${unread} unread` : "Chat"} onClick={toggleChat}>
          <MessageSquare aria-hidden="true" />Chat{unread > 0 && <span className="session-chat-unread" aria-hidden="true">{unread > 99 ? "99+" : unread}</span>}
        </Button>}
        <Button className={showChat ? undefined : "ml-auto"} variant="ghost" size="icon-sm" aria-label="Close session" onClick={onClose}><X aria-hidden="true" /></Button>
      </header>
      {!hasListedSession && session && resolvedFallback?.error && <div className="p-4 text-ui text-muted-foreground">
        <p role="status">Could not refresh this session.</p>
        <Button variant="ghost" onClick={retryFallback}>Try again</Button>
      </div>}
      {session && owner ? <LocalSessionPanel key={sessionViewKey(session)} session={session} ownerName={owner.ownerName} own={owner.own}
        openedConversation={openedConversation}
        client={client} revision={hintRevision} onAccessLost={onAccessLost} participants={withSessions} onMessageOwner={messageOwner} onEvent={onEvent} nameOf={id => resolveSessionOwner(id).ownerName} focusSeq={focus.seq} />
        : <div className="p-4 text-ui text-muted-foreground">
          <p role="status">{resolvedFallback?.notFound ? "This session is no longer shared." : resolvedFallback?.error ? "Could not load this session." : "Opening session…"}</p>
          {resolvedFallback?.error && <Button variant="ghost" onClick={retryFallback}>Try again</Button>}
        </div>}
    </div>
    {chatShown && session && openChat && <SessionChat key={session.sessionId} sessionId={session.sessionId}
      sessionOwnerId={session.humanUserId} openChat={openChat} onClose={toggleChat} onRoom={onChatRoom} onRead={onAccessLost} />}
  </section>;
}
