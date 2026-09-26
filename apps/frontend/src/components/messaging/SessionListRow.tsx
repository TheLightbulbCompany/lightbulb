"use client";

import { sessionLabel, sessionStatus, sessionTitle } from "@lightbulb/shared";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { SessionToolIcon } from "./SessionToolIcon";
import type { SidebarSession } from "./roomSections";

/** One session row: the sidebar's live list and the All sessions view share it. */
export function SessionListRow({ entry: { session, ownerName, own, unread }, current, onSelect }: {
  entry: SidebarSession; current: boolean; onSelect?: (sessionId: string) => void;
}) {
  const label = sessionLabel(session);
  const who = own ? "your session" : ownerName;
  const news = unread === "finished" ? ", finished since you looked" : unread ? ", new output" : "";
  return <li className="room-directory-row room-directory-row--roster">
    <button type="button" className="room-directory-entry" aria-current={current ? "page" : undefined}
      aria-label={`Open ${label}, ${who}, ${sessionStatus[session.status]}${news}`} onClick={() => onSelect?.(session.sessionId)}>
      <span className="session-tool-badge" aria-hidden="true"><SessionToolIcon provider={session.provider} /><span className={`native-session-dot native-session-dot-${session.status}`} /></span>
      <span className="min-w-0 flex-1">
        <span className={`block truncate${unread ? " font-semibold" : ""}`} title={label}>{sessionTitle(session)}</span>
        <span className="block truncate text-xs text-muted-foreground">{own ? "You" : ownerName} · {sessionStatus[session.status]}{session.lastEventAt ? ` · ${timeAgo(session.lastEventAt)}` : ""}</span>
      </span>
      {unread && <span className={`session-unread session-unread--${unread}`} aria-hidden="true" />}
    </button>
  </li>;
}
