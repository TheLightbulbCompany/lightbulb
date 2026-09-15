import type { LocalSession } from "./types";

export const sessionLabel = (s: LocalSession) => `${s.provider === "claude" ? "Claude" : "Codex"} · ${s.title?.trim() || s.sessionId.slice(0, 8)}`;

export const sessionStatus: Record<LocalSession["status"], string> = {
  idle: "Idle", busy: "Working", "waiting-approval": "Waiting for approval on owner's machine", unknown: "Activity unknown", offline: "Offline", ended: "Ended",
};

/** List/sidebar ordering, shared by web and mobile: busy → waiting on the owner → idle-ish → gone. */
export const SESSION_STATUS_RANK: Record<LocalSession["status"], number> = { busy: 0, "waiting-approval": 1, idle: 2, unknown: 2, offline: 3, ended: 3 };

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
