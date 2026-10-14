"use client";

import { useSyncExternalStore } from "react";
import { endedWhy, sessionEndable, sessionLabel, sessionLive, sessionStatus, sessionTitle } from "@lightbulb/shared";
import { SessionToolIcon } from "./SessionToolIcon";
import { SessionRenameButton } from "./SessionRenameButton";
import { SessionRemoveButton } from "./SessionRemoveButton";
import type { SidebarSession } from "./roomSections";

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
 * and the owner when it is someone else's.
 */
export function SessionListRow({ entry: { session, ownerName, own, unread }, current, onSelect, onRename, onRemove }: {
  entry: SidebarSession; current: boolean; onSelect?: (sessionId: string) => void;
  onRename?: (sessionId: string, title: string | null) => Promise<unknown>;
  /** The owner takes a session of their own off the list; a running one is ended first. */
  onRemove?: (sessionId: string) => Promise<unknown>;
}) {
  const label = sessionLabel(session);
  const live = sessionLive(session);
  const why = endedWhy(session, own ? "your Mac" : `${ownerName}'s Mac`);
  const who = own ? "your session" : ownerName;
  const news = unread === "finished" ? ", finished since you looked" : unread ? ", new output" : "";
  const chat = session.chatUnread ? `, ${session.chatUnread} unread in chat` : "";
  const waiting = live && session.status === "waiting-approval";
  const second = [own ? null : ownerName, why ?? session.cwdName].filter(Boolean).join(" · ");
  return <li className={`room-directory-row room-directory-row--roster session-row${live ? "" : " session-row--gone"}`}>
    <button type="button" className="room-directory-entry" aria-current={current ? "page" : undefined}
      aria-label={`Open ${label}, ${who}, ${sessionStatus[session.status]}${news}${chat}`} onClick={() => onSelect?.(session.sessionId)}>
      <span className="session-tool-badge" aria-hidden="true"><SessionToolIcon provider={session.provider} /></span>
      <span className="min-w-0 flex-1">
        <span className={`block truncate${unread ? " font-semibold" : ""}${live && session.status === "busy" ? " session-title--working" : ""}`} title={label}>{sessionTitle(session)}</span>
        {second && <span className="block truncate text-xs text-muted-foreground">{second}</span>}
      </span>
      {!!session.chatUnread && <span className="session-chat-unread" aria-hidden="true">{session.chatUnread > 99 ? "99+" : session.chatUnread}</span>}
      {waiting ? <span className="session-unread session-unread--waiting" aria-hidden="true" />
        : unread ? <span className={`session-unread session-unread--${unread}`} aria-hidden="true" />
          : <SessionListTime at={session.lastEventAt ?? session.endedAt} />}
    </button>
    {own && onRename && <SessionRenameButton session={session} onRename={onRename} icon />}
    {own && onRemove && (session.status === "ended" || sessionEndable(session))
      && <SessionRemoveButton name={sessionTitle(session)} live={session.status !== "ended"} onRemove={() => onRemove(session.sessionId)} icon />}
  </li>;
}
