"use client";

import { useRef, type ReactNode } from "react";
import { X } from "lucide-react";
import { sessionLabel, sessionViewKey, useRoomResources, type RoomResourceSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { ColumnGrip } from "./ColumnGrip";
import { LocalSessionPanel } from "./LocalSessionPanel";
import { useSessionColumnWidth } from "./sidebarPrefs";

export interface SessionFocusTarget { sessionId: string; ownerUserId: string; ownerName: string; roomId: string | null; own: boolean }

/**
 * Focus placement (spec §4): the session fills the middle; for someone else's
 * session the conversation used as context (their DM) sits on the right in the
 * same resizable column the room placement uses. The room is also the
 * authorization context for events and follow-ups, so it must list the session.
 */
export function SessionFocus({ focus, source, onClose, conversation, scope }: {
  focus: SessionFocusTarget; source: RoomResourceSource; onClose: () => void; conversation: ReactNode; scope: string;
}) {
  const resources = useRoomResources(focus.roomId ?? "", source, { enabled: !!focus.roomId, includeEnded: true });
  const session = resources.data?.sessions.find((candidate) => candidate.sessionId === focus.sessionId);
  const panelRef = useRef<HTMLElement>(null);
  const column = useSessionColumnWidth(scope, panelRef);
  return <section ref={panelRef} style={column.style} className="room-panel room-session-focus" aria-label="Shared session">
    <div className="room-session-main">
      <header className="room-context-header">
        {/* The session's own label, as in the room placement's header; who is
            running it is the line directly under it, in the panel summary. */}
        <h3 className="font-semibold room-wrap">{session ? sessionLabel(session) : focus.own ? "Your session" : `${focus.ownerName}'s session`}</h3>
        <Button variant="ghost" size="icon-sm" aria-label="Close session" className="ml-auto" onClick={onClose}><X aria-hidden="true" /></Button>
      </header>
      {!focus.roomId && <p role="status" className="p-4 text-sm text-muted-foreground">Open a conversation first so this session has somewhere to report.</p>}
      {focus.roomId && !session && <p role="status" className="p-4 text-sm text-muted-foreground">{resources.loading ? "Loading session…" : resources.error ?? "This session is no longer shared."}</p>}
      {focus.roomId && session && <LocalSessionPanel key={sessionViewKey(session, resources.accessRevision)}
        session={session} ownerName={focus.ownerName} own={focus.own} roomId={focus.roomId} client={source.client} revision={resources.revision} onAccessLost={resources.revoke} />}
    </div>
    {!focus.own && focus.roomId && <aside className="room-context room-session-side" aria-label="Conversation">
      <ColumnGrip {...column.grip} />
      {conversation}
    </aside>}
  </section>;
}
