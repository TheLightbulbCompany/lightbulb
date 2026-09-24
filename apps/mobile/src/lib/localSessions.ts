import type { RoomDetail } from "@lightbulb/shared";

type RoomMember = RoomDetail["members"][number];

/** A room member's display name by human id; the web's fallback copy when unknown. */
export const memberName = (members: readonly RoomMember[] | undefined, humanUserId: string): string =>
  members?.find((m) => m.kind === "human" && m.id === humanUserId)?.displayName ?? "Room member";
