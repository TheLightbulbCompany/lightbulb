import type { LocalSession } from "./types";

export const sessionLabel = (s: LocalSession) => `${s.provider === "claude" ? "Claude" : "Codex"} · ${s.title?.trim() || s.sessionId.slice(0, 8)}`;

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

/** The access-epoch remount key for a shared session's panel/body: a fresh
 *  generation or access grant is a fresh mount, everywhere one is rendered. */
export const sessionViewKey = (session: Pick<LocalSession, "sessionId" | "generation">, accessRevision: number): string =>
  `${session.sessionId}:${session.generation}:${accessRevision}`;

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
