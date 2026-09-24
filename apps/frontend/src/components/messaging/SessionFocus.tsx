"use client";

import { X } from "lucide-react";
import { sessionLabel, sessionViewKey, type SessionSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { LocalSessionPanel } from "./LocalSessionPanel";
import type { SidebarSession } from "./roomSections";

export interface SessionFocusTarget { sessionId: string }

/**
 * Focus placement: a shared session lives in the workspace, not in a room
 * (owner ruling 2026-09-24), so it fills the middle of the Messages panel on
 * its own. The workspace session list is the one source of what is shared.
 */
export function SessionFocus({ focus, entries, loading, hintRevision, client, onAccessLost, onClose }: {
  focus: SessionFocusTarget; entries: readonly SidebarSession[]; loading: boolean; hintRevision: number;
  client: SessionSource["client"]; onAccessLost: () => void; onClose: () => void;
}) {
  const entry = entries.find((candidate) => candidate.session.sessionId === focus.sessionId);
  return <section className="room-panel room-session-focus" aria-label="Shared session">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold room-wrap">{entry ? sessionLabel(entry.session) : "Shared session"}</h3>
        <Button variant="ghost" size="icon-sm" aria-label="Close session" className="ml-auto" onClick={onClose}><X aria-hidden="true" /></Button>
      </header>
      {entry ? <LocalSessionPanel key={sessionViewKey(entry.session, 0)} session={entry.session} ownerName={entry.ownerName} own={entry.own}
        client={client} revision={hintRevision} onAccessLost={onAccessLost} />
        : <p role="status" className="p-4 text-sm text-muted-foreground">{loading && entries.length === 0 ? "Loading session…" : "This session is no longer shared."}</p>}
    </div>
  </section>;
}
