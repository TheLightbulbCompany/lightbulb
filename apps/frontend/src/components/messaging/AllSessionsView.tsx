"use client";

import { useMemo, useState } from "react";
import { X } from "lucide-react";
import { sessionHistory, sessionMatches } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { SessionListRow } from "./SessionListRow";
import { SessionRemoveButton } from "./SessionRemoveButton";
import type { SidebarSession } from "./roomSections";

/**
 * Every session the workspace lists -- live, offline and recently ended -- in the
 * main pane: searchable, newest activity first, by day. The sidebar shows only
 * live ones; this is where the rest live.
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
  const groups = useMemo(() => {
    const byId = new Map(entries.map((entry) => [entry.session.sessionId, entry]));
    const matching = entries.filter((entry) => sessionMatches(entry.session, query, entry.ownerName)).map((entry) => entry.session);
    return sessionHistory(matching).map((group) => ({ ...group, rows: group.data.map((session) => byId.get(session.sessionId)!) }));
  }, [entries, query]);
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
        {groups.map((group) => <section key={group.key} aria-label={group.title}>
          <h4 className="all-sessions-day">{group.title}</h4>
          <ul>{group.rows.map((entry) => <SessionListRow key={entry.session.sessionId} entry={entry} current={false} onSelect={onOpenSession} onRename={onRename} onRemove={onRemove} />)}</ul>
        </section>)}
        {groups.length === 0 && <p role="status" className="text-sm text-muted-foreground">
          {loading && entries.length === 0 ? "Loading sessions…" : query.trim() ? "No sessions match." : "No sessions yet."}</p>}
      </div>
    </div>
  </section>;
}
