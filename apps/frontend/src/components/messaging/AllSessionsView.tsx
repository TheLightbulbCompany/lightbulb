"use client";

import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, X } from "lucide-react";
import { groupByDay, type SessionRow } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { SessionListRow } from "./SessionListRow";
import { SessionRemoveButton } from "./SessionRemoveButton";
import type { SidebarRow } from "./roomSections";
import { sessionRowAt, sessionRowDropped, sessionRowGone, sessionRowKey, sessionRowMatches } from "./sessionGroups";

/**
 * Every session the workspace lists, in the main pane, laid out like Happy's: the live ones
 * first, newest activity first, with no headings; the ones that have stopped under one
 * collapsed "Ended", by day. A session offline for days is not listed at all (its Mac lost it). Network rows with no backend list too, inert.
 */
export function AllSessionsView({ entries, loading, onOpenSession, onClose, onRename, onRemove, onRemoveEnded, onShare }: {
  entries: readonly SidebarRow[]; loading: boolean;
  onOpenSession: (sessionId: string) => void; onClose: () => void;
  onRename?: (sessionId: string, title: string | null) => Promise<unknown>;
  /** The viewer's own ended sessions can leave the list: one by one, or all at once. */
  onRemove?: (sessionId: string) => Promise<unknown>;
  onRemoveEnded?: () => Promise<unknown>;
  onShare?: (row: SessionRow) => Promise<unknown>;
}) {
  const [query, setQuery] = useState("");
  const [showEnded, setShowEnded] = useState(false);
  const { live, ended, endedCount } = useMemo(() => {
    const matching = entries.filter((entry) => !sessionRowDropped(entry.row) && sessionRowMatches(entry.row, query));
    const stopped = groupByDay(matching.filter((entry) => sessionRowGone(entry.row)), (entry) => sessionRowAt(entry.row));
    return {
      live: matching.filter((entry) => !sessionRowGone(entry.row)).sort((a, b) => sessionRowAt(b.row) - sessionRowAt(a.row)),
      ended: stopped.map((group) => ({ ...group, rows: group.data })),
      endedCount: stopped.reduce((sum, group) => sum + group.data.length, 0),
    };
  }, [entries, query]);
  // A search looks everywhere: what it finds is shown, ended or not.
  const open = showEnded || !!query.trim();
  const row = (entry: SidebarRow) => <SessionListRow key={sessionRowKey(entry.row)} entry={entry} onShare={onShare && (() => onShare(entry.row))} current={false} onSelect={onOpenSession} onRename={onRename} onRemove={onRemove} />;
  return <section className="room-panel room-session-focus" aria-label="All sessions">
    <div className="room-session-main">
      <header className="room-context-header">
        <h3 className="font-semibold">All sessions</h3>
        <span className="ml-auto" />
        {onRemoveEnded && entries.filter((entry) => entry.row.person.isViewer && entry.row.backend?.status === "ended").length > 1 && <SessionRemoveButton onRemove={onRemoveEnded} />}
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
        {live.length === 0 && endedCount === 0 && <p role="status" className="text-ui text-muted-foreground">
          {loading && entries.length === 0 ? "Loading sessions…" : query.trim() ? "No sessions match." : "No sessions yet."}</p>}
      </div>
    </div>
  </section>;
}
