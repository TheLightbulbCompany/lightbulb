"use client";

import { useState, useSyncExternalStore } from "react";
import { Archive, ArchiveRestore, MailOpen, MoreHorizontal, Pin, PinOff } from "lucide-react";
import { endedWhy, sessionEndable, sessionLabel, sessionLive, sessionSignalsLine, sessionState, sessionStateLabel, sessionTitle, sessionWaitingLine } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { SessionToolIcon } from "./SessionToolIcon";
import { SessionRenameButton } from "./SessionRenameButton";
import { SessionRemoveButton } from "./SessionRemoveButton";
import { ParticipantAvatar } from "./ParticipantAvatar";
import type { SidebarSession } from "./roomSections";

export interface SessionRowMarks {
  pinned: boolean;
  archived: boolean;
  onTogglePin: () => void;
  onToggleArchive: () => void;
  onMarkUnread: (unread: boolean) => void;
}

/** Happy's list clock, by the viewer's calendar: a time today, a weekday within the week, then a date. */
export function sessionListTime(iso: string | null | undefined, now = Date.now()): string {
  const at = Date.parse(iso ?? "");
  if (!at) return "";
  const date = new Date(at);
  const midnight = new Date(now);
  midnight.setHours(0, 0, 0, 0);
  if (at >= midnight.getTime()) return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  midnight.setDate(midnight.getDate() - 6);
  if (at >= midnight.getTime()) return date.toLocaleDateString([], { weekday: "short" });
  const thisYear = date.getFullYear() === new Date(now).getFullYear();
  return date.toLocaleDateString([], thisYear ? { month: "numeric", day: "numeric" } : { month: "numeric", day: "numeric", year: "2-digit" });
}

const noSubscribe = () => () => {};

/** The viewer's own clock and locale, so it waits for the browser: the server's would not match. */
function SessionListTime({ at }: { at: string | null | undefined }) {
  const browser = useSyncExternalStore(noSubscribe, () => true, () => false);
  return <span className="session-row-time" aria-hidden="true">{browser ? sessionListTime(at) : ""}</span>;
}

/**
 * One session row: the sidebar's live list and the All sessions view share it. Shaped like
 * Happy's (owner ruling 2026-10-13): no status words. The title shimmers while the session
 * works, an orange dot replaces the time while it waits on its person, a blue one while it has
 * output nobody looked at, and a session that is gone is faded. The second line is the folder,
 * and the owner when it is someone else's; `oneLine` (the sidebar) drops it, and the
 * accessible label still carries both.
 */
export function SessionListRow({ entry: { session, ownerName, own, owner, unread }, current, onSelect, onRename, onRemove, oneLine = false, marks }: {
  entry: SidebarSession; current: boolean; onSelect?: (sessionId: string) => void;
  oneLine?: boolean;
  onRename?: (sessionId: string, title: string | null) => Promise<unknown>;
  /** The owner takes a session of their own off the list; a running one is ended first. */
  onRemove?: (sessionId: string) => Promise<unknown>;
  /** The sidebar's per-viewer row actions (Pin, Mark unread/read, Archive), kept on this browser. */
  marks?: SessionRowMarks;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const label = sessionLabel(session);
  const live = sessionLive(session);
  const why = endedWhy(session, own ? "your Mac" : `${ownerName}'s Mac`);
  const who = own ? "your session" : ownerName;
  const news = unread === "finished" ? ", finished since you looked" : unread ? ", new output" : "";
  const chat = session.chatUnread ? `, ${session.chatUnread} unread in chat` : "";
  // An open prompt or a job's hand-off note (spec 2026-10-26 (e)): the dot, and the note as its preview.
  const waiting = sessionState(session) === "waiting";
  const waitLine = sessionWaitingLine(session);
  const where = why ?? (session.cwdName ? `in ${session.cwdName}` : null);
  // Running subagents and the branch's PR (spec 2026-10-26 (f) gaps 16, 15): live rows only.
  const signals = live ? sessionSignalsLine(session) : null;
  const second = oneLine ? "" : [own ? null : ownerName, why ?? waitLine ?? session.cwdName, signals].filter(Boolean).join(" · ");
  return <li className={`room-directory-row room-directory-row--roster session-row${live ? "" : " session-row--gone"}`}
    onContextMenu={marks ? (event) => { event.preventDefault(); setMenuOpen(true); } : undefined}>
    <button type="button" className="room-directory-entry" aria-current={current ? "page" : undefined}
      aria-label={`Open ${label}, ${who}${marks?.pinned ? ", pinned" : ""}${where ? `, ${where}` : ""}, ${sessionStateLabel(session)}${waitLine && waitLine !== sessionStateLabel(session) ? `, ${waitLine}` : ""}${signals ? `, ${signals}` : ""}${news}${chat}`} onClick={() => onSelect?.(session.sessionId)}>
      <span className="session-owner-face">
        <ParticipantAvatar participant={owner} size="sm" showPresence={false} />
        <span className="session-tool-badge session-tool-badge--corner" aria-hidden="true"><SessionToolIcon provider={session.provider} /></span>
      </span>
      <span className="min-w-0 flex-1">
        <span className={`block truncate${unread ? " font-semibold" : ""}${live && session.status === "busy" ? " session-title--working" : ""}`} title={label}>{sessionTitle(session)}{oneLine && signals && <span className="text-caption text-muted-foreground" aria-hidden="true"> · {signals}</span>}</span>
        {second && <span className="block truncate text-caption text-muted-foreground">{second}</span>}
      </span>
      {!!session.chatUnread && <span className="session-chat-unread" aria-hidden="true">{session.chatUnread > 99 ? "99+" : session.chatUnread}</span>}
      {waiting ? <span className="session-unread session-unread--waiting" aria-hidden="true" />
        : unread ? <span className={`session-unread session-unread--${unread}`} aria-hidden="true" />
          : <SessionListTime at={session.lastEventAt ?? session.endedAt} />}
    </button>
    {marks && <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" className="room-directory-actions" aria-label={`Session actions for ${sessionTitle(session)}`}><MoreHorizontal aria-hidden="true" /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {marks.archived
          ? <DropdownMenuItem onSelect={marks.onToggleArchive}><ArchiveRestore aria-hidden="true" />Unarchive</DropdownMenuItem>
          : <>
            <DropdownMenuItem onSelect={marks.onTogglePin}>{marks.pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}{marks.pinned ? "Unpin" : "Pin"}</DropdownMenuItem>
            {/* The open session reads itself at once, so Mark unread is offered on the others only. */}
            {(unread || !current) && <DropdownMenuItem onSelect={() => marks.onMarkUnread(!unread)}><MailOpen aria-hidden="true" />{unread ? "Mark read" : "Mark unread"}</DropdownMenuItem>}
            <DropdownMenuItem onSelect={marks.onToggleArchive}><Archive aria-hidden="true" />Archive</DropdownMenuItem>
          </>}
      </DropdownMenuContent>
    </DropdownMenu>}
    {own && onRename && <SessionRenameButton session={session} onRename={onRename} icon />}
    {own && onRemove && (session.status === "ended" || sessionEndable(session))
      && <SessionRemoveButton name={sessionTitle(session)} live={session.status !== "ended"} onRemove={() => onRemove(session.sessionId)} icon />}
  </li>;
}
