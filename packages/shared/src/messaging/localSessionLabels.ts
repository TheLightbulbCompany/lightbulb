import type { LocalSession } from "./types";

const TOOL_NAMES: Record<string, string> = { claude: "Claude", codex: "Codex" };
/** The coding tool's display name; an unknown future provider shows its raw id. */
export const sessionToolName = (provider: string): string => TOOL_NAMES[provider] ?? provider;
/** The title without the tool prefix, for surfaces that show the tool as a logo. */
// Never an id: VoiceOver reads it aloud as gibberish.
export const sessionTitle = (s: LocalSession): string =>
  s.displayTitle?.trim() || s.title?.trim() || s.cwdName?.trim() || `${sessionToolName(s.provider)} session`;
export const sessionLabel = (s: LocalSession) => `${sessionToolName(s.provider)} · ${sessionTitle(s)}`;

export const sessionStatus: Record<LocalSession["status"], string> = {
  idle: "Idle", busy: "Working", "waiting-approval": "Waiting for input", unknown: "No recent activity", offline: "Offline", ended: "Ended",
};

/** List/sidebar ordering, shared by web and mobile: busy → waiting on the owner → idle-ish → gone. */
const SESSION_STATUS_RANK: Record<LocalSession["status"], number> = { busy: 0, "waiting-approval": 1, idle: 2, unknown: 2, offline: 3, ended: 3 };

export const unavailableReason: Record<NonNullable<LocalSession["controlUnavailableReason"]>, string> = {
  "runtime-unverified": "This session's native runtime has not been verified.",
  "endpoint-unavailable": "The native input connection is unavailable.",
  "unsupported-version": "This provider version does not support verified control.",
  "history-gap": "History has a gap. Control is unavailable until the desktop reconnects safely.",
};

/** Room targets require a live session; only direct composers opt into offline queuing. */
export const canSendToSession = (session: LocalSession, allowOffline = false): boolean =>
  session.status !== "ended" && session.capabilitiesVerified &&
  !session.controlUnavailableReason && session.capabilities.sendMode !== "none" &&
  (session.status !== "offline" || (allowOffline && session.capabilities.sendMode === "queue")) &&
  !(session.status === "busy" && session.capabilities.sendMode === "immediate" && !session.activeTurnId);

/** Why the owner cannot send right now, shared by web and mobile; null when sending is available. */
export const sendBlockedReason = (session: LocalSession): string | null =>
  canSendToSession(session, true) ? null
    : session.controlUnavailableReason ? unavailableReason[session.controlUnavailableReason]
      : session.status === "offline" ? "This session cannot queue a follow-up while the Mac is offline."
        : "This session does not accept remote input.";

/** The remount key for a shared session's panel/body: a fresh generation is a
 *  fresh mount, everywhere one is rendered. */
export const sessionViewKey = (session: Pick<LocalSession, "sessionId" | "generation">): string =>
  `${session.sessionId}:${session.generation}`;

export interface WorkspaceSection { key: string; title: string; data: LocalSession[] }

/** The sidebar's grouping, shared by web and mobile: one section per person, mine
 *  first; within a person busy → waiting → idle, then by title. Not by last
 *  output: sessions in the same state then traded places every time one of them
 *  wrote something, and the row about to be clicked moved. */
export function workspaceSections(sessions: readonly LocalSession[], currentUserId: string | null | undefined, name: (humanUserId: string) => string): WorkspaceSection[] {
  const humans = [...new Set(sessions.map((s) => s.humanUserId))];
  const title = (human: string) => (human === currentUserId ? "You" : name(human));
  humans.sort((a, b) => Number(b === currentUserId) - Number(a === currentUserId) || title(a).localeCompare(title(b)));
  return humans.map((human) => ({
    key: human, title: title(human),
    data: sessions.filter((s) => s.humanUserId === human).sort((a, b) => SESSION_STATUS_RANK[a.status] - SESSION_STATUS_RANK[b.status]
      || sessionTitle(a).localeCompare(sessionTitle(b)) || a.sessionId.localeCompare(b.sessionId)),
  }));
}

/** Offline this long and a session stops counting: no unread dot, no "sharing" presence. Computed at read time, never stored. */
export const SESSION_ARCHIVE_AFTER_MS = 3 * 24 * 60 * 60 * 1000;

/** Offline, and last heard from at least `SESSION_ARCHIVE_AFTER_MS` ago. It stops being so the moment it reconnects. */
export const sessionArchived = (session: LocalSession, now = Date.now()): boolean =>
  session.status === "offline" && !!session.lastEventAt && now - Date.parse(session.lastEventAt) >= SESSION_ARCHIVE_AFTER_MS;

/** The desktop is mirroring it right now (working, waiting, idle, or activity unknown): the only sessions the sidebar lists. */
export const sessionLive = (session: LocalSession): boolean => session.status !== "offline" && session.status !== "ended";

/** How many live sessions the sidebar shows before "+N more". */
export const SIDEBAR_SESSION_CAP = 5;

export interface SessionDayGroup { key: "today" | "yesterday" | "week" | "earlier"; title: string; data: LocalSession[] }

/** The "All sessions" list, shared by web and mobile: newest activity first, bucketed by the viewer's local day. */
export function sessionHistory(sessions: readonly LocalSession[], now = Date.now()): SessionDayGroup[] {
  const at = (s: LocalSession) => Date.parse(s.lastEventAt ?? s.endedAt ?? "") || 0;
  const day = new Date(now);
  day.setHours(0, 0, 0, 0);
  const today = day.getTime();
  day.setDate(day.getDate() - 1);
  const yesterday = day.getTime();
  day.setDate(day.getDate() - 5);
  const week = day.getTime();
  const groups: SessionDayGroup[] = [
    { key: "today", title: "Today", data: [] }, { key: "yesterday", title: "Yesterday", data: [] },
    { key: "week", title: "This week", data: [] }, { key: "earlier", title: "Earlier", data: [] },
  ];
  for (const s of [...sessions].sort((a, b) => at(b) - at(a))) {
    const t = at(s);
    groups[t >= today ? 0 : t >= yesterday ? 1 : t >= week ? 2 : 3].data.push(s);
  }
  return groups.filter((group) => group.data.length > 0);
}

/** Per viewer, per session: the `latestEventSeq` last seen. Kept on the device (a convenience, not state). */
export type SessionSeen = Readonly<Record<string, number>>;

/**
 * New output since the viewer last looked: `finished` once the session stopped working
 * (idle or ended), else `unread`. A session never seen has no baseline and shows nothing:
 * `markSessionsSeen` sets one the first time the list shows it. An archived session never shows one.
 */
export function sessionUnread(session: LocalSession, seen: SessionSeen, now = Date.now()): "unread" | "finished" | null {
  const last = seen[session.sessionId];
  if (last === undefined || sessionArchived(session, now) || (session.latestEventSeq ?? 0) <= last) return null;
  return session.status === "idle" || session.status === "ended" ? "finished" : "unread";
}

/** Baselines sessions seen for the first time and advances the open one. Returns `seen` itself when nothing moved. */
export function markSessionsSeen(seen: SessionSeen, sessions: readonly LocalSession[], openSessionId: string | null): SessionSeen {
  let next: Record<string, number> | null = null;
  for (const s of sessions) {
    const seq = s.latestEventSeq ?? 0;
    const last = seen[s.sessionId];
    if (last === undefined || (s.sessionId === openSessionId && seq > last)) {
      next ??= { ...seen };
      next[s.sessionId] = seq;
    }
  }
  return next ?? seen;
}

/** The sidebar/mobile filter: title, folder, tool or owner, case-insensitive. */
export function sessionMatches(session: LocalSession, query: string, ownerName = ""): boolean {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  return [sessionTitle(session), session.cwdName ?? "", sessionToolName(session.provider), ownerName]
    .some((field) => field.toLocaleLowerCase().includes(needle));
}
