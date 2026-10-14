"use client";

import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, X } from "lucide-react";
import { sessionArchived, sessionHistory, sessionLive, sessionMatches } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { SessionListRow } from "./SessionListRow";
import { SessionRemoveButton } from "./SessionRemoveButton";
import type { SidebarSession } from "./roomSections";

/**
 * Every session the workspace lists, in the main pane, laid out like Happy's: the live ones
 * first, newest activity first, with no headings; the ones that have stopped under one
 * collapsed "Ended", by day. A session offline for days is not listed at all (its Mac lost it).
 */
export function AllSessionsView({ entries, loading, onOpenSession, onClose, onRename, onRemove, onRemoveEnded }: {
  entries: readonly SidebarSession[]; loading: boolean;
  onOpenSession: (sessionId: string) => void; onClose: () => void;
  onRename?: (sessionId: string, title: string | null) => Promise<unknown>;
  /** The viewer's own ended sessions can leave the list: one by one, or all at once. */
  onRemove?: (sessionId: string) => Promise<unknown>;
  onRemoveEnded?: () => Promise<unknown>;
}) {
  const [query, setQuery] = useState("");
  const [showEnded, setShowEnded] = useState(false);
  const { live, ended, endedCount } = useMemo(() => {
    const byId = new Map(entries.map((entry) => [entry.session.sessionId, entry]));
    const rows = (sessions: { sessionId: string }[]) => sessions.map((session) => byId.get(session.sessionId)!);
    const matching = entries.filter((entry) => !sessionArchived(entry.session) && sessionMatches(entry.session, query, entry.ownerName))
      .map((entry) => entry.session);
    const stopped = sessionHistory(matching.filter((session) => !sessionLive(session)));
    return {
      live: rows(sessionHistory(matching.filter(sessionLive)).flatMap((group) => group.data)),
      ended: stopped.map((group) => ({ ...group, rows: rows(group.data) })),
      endedCount: stopped.reduce((sum, group) => sum + group.data.length, 0),
    };
  }, [entries, query]);
  // A search looks everywhere: what it finds is shown, ended or not.
  const open = showEnded || !!query.trim();
  const row = (entry: SidebarSession) => <SessionListRow key={entry.session.sessionId} entry={entry} current={false} onSelect={onOpenSession} onRename={onRename} onRemove={onRemove} />;
  return <section className="room-panel room-session-focus" aria-label="All sessions">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold">All sessions</h3>
        <span className="ml-auto" />
        {onRemoveEnded && entries.filter((entry) => entry.own && entry.session.status === "ended").length > 1 && <SessionRemoveButton onRemove={onRemoveEnded} />}
        <Button variant="ghost" size="icon-sm" aria-label="Close all sessions" onClick={onClose}><X aria-hidden="true" /></Button>
      </header>
      <div className="all-sessions">
        <input type="search" className="all-sessions-search" placeholder="Search sessions" aria-label="Search sessions"
          value={query} onChange={(event) => setQuery(event.target.value)} />
        {live.length > 0 && <ul aria-label="Live">{live.map(row)}</ul>}
        {endedCount > 0 && <section aria-label="Ended">
          <button type="button" className="all-sessions-toggle" aria-expanded={open} onClick={() => setShowEnded((shown) => !shown)}>
            {open ? <ChevronDown aria-hidden="true" /> : <ChevronRight aria-hidden="true" />}Ended <span className="text-muted-foreground">{endedCount}</span>
          </button>
          {open && ended.map((group) => <section key={group.key} aria-label={group.title}>
            <h4 className="all-sessions-day">{group.title}</h4>
            <ul>{group.rows.map(row)}</ul>
          </section>)}
        </section>}
        {live.length === 0 && endedCount === 0 && <p role="status" className="text-sm text-muted-foreground">
          {loading && entries.length === 0 ? "Loading sessions…" : query.trim() ? "No sessions match." : "No sessions yet."}</p>}
      </div>
    </div>
  </section>;
}
