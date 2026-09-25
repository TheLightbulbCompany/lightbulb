import type { LocalSession } from "./types";

const TOOL_NAMES: Record<string, string> = { claude: "Claude", codex: "Codex" };
/** The coding tool's display name; an unknown future provider shows its raw id. */
export const sessionToolName = (provider: string): string => TOOL_NAMES[provider] ?? provider;
/** The title without the tool prefix, for surfaces that show the tool as a logo. */
export const sessionTitle = (s: LocalSession): string => s.title?.trim() || s.sessionId.slice(0, 8);
export const sessionLabel = (s: LocalSession) => `${sessionToolName(s.provider)} · ${sessionTitle(s)}`;

export const sessionStatus: Record<LocalSession["status"], string> = {
  idle: "Idle", busy: "Working", "waiting-approval": "Waiting for input", unknown: "Activity unknown", offline: "Offline", ended: "Ended",
};

/** List/sidebar ordering, shared by web and mobile: busy → waiting on the owner → idle-ish → gone. */
const SESSION_STATUS_RANK: Record<LocalSession["status"], number> = { busy: 0, "waiting-approval": 1, idle: 2, unknown: 2, offline: 3, ended: 3 };

export const unavailableReason: Record<NonNullable<LocalSession["controlUnavailableReason"]>, string> = {
  "runtime-unverified": "This session's native runtime has not been verified.",
  "endpoint-unavailable": "The native input connection is unavailable.",
  "unsupported-version": "This provider version does not support verified control.",
  "history-gap": "History has a gap. Control is unavailable until the desktop reconnects safely.",
};

/** Verified control, a send mode, and — when input is immediate — a known turn. */
export const canSendToSession = (session: LocalSession): boolean =>
  session.status !== "offline" && session.status !== "ended" && session.capabilitiesVerified &&
  !session.controlUnavailableReason && session.capabilities.sendMode !== "none" &&
  !(session.status === "busy" && session.capabilities.sendMode === "immediate" && !session.activeTurnId);

/** Why the owner cannot send right now, shared by web and mobile; null when sending is available. */
export const sendBlockedReason = (session: LocalSession): string | null =>
  canSendToSession(session) ? null
    : session.status === "offline" ? "Offline. Sending resumes when the Mac reconnects."
      : session.controlUnavailableReason ? unavailableReason[session.controlUnavailableReason]
        : "This session does not accept remote input.";

/** The remount key for a shared session's panel/body: a fresh generation is a
 *  fresh mount, everywhere one is rendered. */
export const sessionViewKey = (session: Pick<LocalSession, "sessionId" | "generation">): string =>
  `${session.sessionId}:${session.generation}`;

export interface WorkspaceSection { key: string; title: string; data: LocalSession[] }

/** The sidebar's grouping, shared by web and mobile: one section per person, mine
 *  first; within a person busy → waiting → idle, then most recent. */
export function workspaceSections(sessions: readonly LocalSession[], currentUserId: string | null | undefined, name: (humanUserId: string) => string): WorkspaceSection[] {
  const humans = [...new Set(sessions.map((s) => s.humanUserId))];
  const title = (human: string) => (human === currentUserId ? "You" : name(human));
  humans.sort((a, b) => Number(b === currentUserId) - Number(a === currentUserId) || title(a).localeCompare(title(b)));
  return humans.map((human) => ({
    key: human, title: title(human),
    data: sessions.filter((s) => s.humanUserId === human).sort((a, b) => SESSION_STATUS_RANK[a.status] - SESSION_STATUS_RANK[b.status] || (b.lastEventAt ?? "").localeCompare(a.lastEventAt ?? "")),
  }));
}

/** Per viewer, per session: the `latestEventSeq` last seen. Kept on the device (a convenience, not state). */
export type SessionSeen = Readonly<Record<string, number>>;

/**
 * New output since the viewer last looked: `finished` once the session stopped working
 * (idle or ended), else `unread`. A session never seen has no baseline and shows nothing:
 * `markSessionsSeen` sets one the first time the list shows it.
 */
export function sessionUnread(session: LocalSession, seen: SessionSeen): "unread" | "finished" | null {
  const last = seen[session.sessionId];
  if (last === undefined || (session.latestEventSeq ?? 0) <= last) return null;
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
