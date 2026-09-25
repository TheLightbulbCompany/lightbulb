import { markSessionsSeen, type LocalSession, type RoomDetail, type SessionSeen } from "@lightbulb/shared";

import { LocalStore } from "./localStore";

type RoomMember = RoomDetail["members"][number];

/** A room member's display name by human id; the web's fallback copy when unknown. */
export const memberName = (members: readonly RoomMember[] | undefined, humanUserId: string): string =>
  members?.find((m) => m.kind === "human" && m.id === humanUserId)?.displayName ?? "Room member";

const seenStore = new LocalStore<Record<string, number>>("session-seen");

/** This device's last-seen seq per shared session (the list's unread dot). */
export const readSessionSeen = (): SessionSeen => seenStore.read();

/** Baselines first-seen sessions and advances the open one; see `markSessionsSeen` in shared. */
export function markSessionsSeenOnDevice(sessions: readonly LocalSession[], openSessionId: string | null): SessionSeen {
  const seen = seenStore.read();
  const next = markSessionsSeen(seen, sessions, openSessionId);
  if (next !== seen) seenStore.write({ ...next });
  return next;
}
