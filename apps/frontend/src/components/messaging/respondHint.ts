import type { ListenMode, MemberRef, RoomKind } from "@lightbulb/shared";

/**
 * What people see for each stored listen mode. The stored values predate these
 * names: `active` shows as "Observe" (a cheap model decides whether an
 * unmentioned message gets a reply) and `observe` as "Off" (never runs).
 */
export const LISTEN_MODE_LABELS: Record<ListenMode, string> = { active: "Observe", mentions: "Mentions", observe: "Off" };

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
    // observe ("Off") never runs — not on ambient traffic, not when mentioned, not in a DM.
    if (member.listenMode === "observe") { parts.push(`${member.displayName} is off`); continue; }
    // The dispatch decision comes FIRST: an agent this message will not be
    // dispatched to cannot be steered by it, however busy it looks.
    const addressed = roomKind === "dm" || mentioned.has(member.id);
    const willReply = addressed || member.listenMode === "active";
    if (!willReply) parts.push(`${member.displayName} when mentioned`);
    else if (runningAgentIds.includes(member.id)) parts.push(`${member.displayName} is working — sending now steers that run`);
    // An unaddressed active ("Observe") agent passes the cheap relevance gate first.
    else parts.push(`${member.displayName} ${addressed ? "will" : "may"} reply`);
  }
  return parts.join(" · ");
}
