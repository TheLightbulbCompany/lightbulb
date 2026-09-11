import type { ListenMode, MemberRef, RoomKind } from "@lightbulb/shared";

export interface RespondTarget {
  kind: "human" | "agent";
  id: string;
  displayName: string;
  listenMode: ListenMode | null;
}

/**
 * Mirrors select_dispatch_targets (apps/backend/core/services/messaging.py).
 * Derived ONLY from room kind, listen modes, the draft's structured mentions,
 * and live run state, so it can never disagree with what the backend will
 * actually do.
 */
export function respondHint(
  roomKind: RoomKind | undefined,
  members: readonly RespondTarget[],
  mentions: readonly MemberRef[],
  runningAgentIds: readonly string[],
): string {
  const mentioned = new Set(mentions.filter((ref) => ref.kind === "agent").map((ref) => ref.id));
  const parts: string[] = [];
  for (const member of members) {
    if (member.kind !== "agent") continue;
    // observe never runs — not on ambient traffic, not when mentioned, not in a DM.
    if (member.listenMode === "observe") parts.push(`${member.displayName} observing`);
    else if (runningAgentIds.includes(member.id)) parts.push(`${member.displayName} is working — sending now steers that run`);
    else if (roomKind === "dm" || mentioned.has(member.id) || member.listenMode === "active") parts.push(`${member.displayName} will reply`);
    else parts.push(`${member.displayName} when mentioned`);
  }
  return parts.join(" · ");
}
