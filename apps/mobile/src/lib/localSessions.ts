import type { LocalSession, RoomDetail } from "@lightbulb/shared";

type RoomMember = RoomDetail["members"][number];

/** Sessions a reader would want to glance at now: working, or blocked on the owner. */
export const liveSessionCount = (sessions: readonly LocalSession[] | undefined): number =>
  (sessions ?? []).filter((s) => s.status === "busy" || s.status === "waiting-approval").length;

/** A room member's display name by human id; the web's fallback copy when unknown. */
export const memberName = (members: readonly RoomMember[] | undefined, humanUserId: string): string =>
  members?.find((m) => m.kind === "human" && m.id === humanUserId)?.displayName ?? "Room member";
