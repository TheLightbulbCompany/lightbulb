import type { ListenMode, MemberRef, RoomKind } from "./types";

/**
 * What people see for each stored listen mode. The stored values predate these
 * names: `active` shows as "Listening" (the default on every plan — every agent
 * listens, and Jev, a routing model, picks at most one responder per message)
 * and `observe` as "Off" (never runs). No label reuses a stored value, so the
 * two vocabularies cannot be confused.
 */
export const LISTEN_MODE_LABELS: Record<ListenMode, string> = { active: "Listening", mentions: "Mentions", observe: "Off" };

/** The Response `<select>` options. */
export function listenModeOptions(): { value: ListenMode; label: string }[] {
  return (Object.entries(LISTEN_MODE_LABELS) as [ListenMode, string][]).map(([value, label]) => ({ value, label }));
}

/**
 * Who may change an agent's listen mode in a room (owner ruling 2026-09-24,
 * every plan): a room owner/admin always, and any other member. Given the
 * room, a DM is excluded (the mode is inert there) and so is the
 * vendor-shared room's any-member path (a Lightbulb-side member would spend
 * the customer's inference). That is a client-side tightening: the backend
 * lets the room owner's own members change it. Web passes no room.
 */
export function canChangeListenMode(
  viewerRole: string | null | undefined,
  room?: { kind: RoomKind; shared?: boolean },
): boolean {
  if (room?.kind === "dm") return false;
  const admin = viewerRole === "owner" || viewerRole === "admin";
  return admin || (!!viewerRole && !room?.shared);
}

/**
 * The viewer owns this channel and is the only person in it. The server refuses
 * that leave (with nobody to hand the channel to, it must be deleted instead);
 * every other channel owner leaves and the server hands ownership on.
 */
export function soleChannelOwner(
  room: { kind: RoomKind; members?: readonly { kind: string; id: string; role?: string | null }[] } | null | undefined,
  userId: string | null | undefined,
): boolean {
  if (room?.kind !== "channel" || !userId) return false;
  const humans = (room.members ?? []).filter((member) => member.kind === "human");
  return humans.some((member) => member.id === userId && member.role === "owner")
    && !humans.some((member) => member.id !== userId);
}

export interface RespondTarget {
  kind: "human" | "agent";
  id: string;
  displayName: string;
  listenMode: ListenMode | null;
  mentionHandle?: string | null;
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
  /** Super-agent on: Lightbulb (`@lightbulb`) is the one voice; seated specialists never answer on their own. */
  oneVoice = false,
): string {
  if (oneVoice) {
    const front = members.find((m) => m.kind === "agent" && m.mentionHandle === "lightbulb");
    // No @lightbulb on the row yet (an older seat): still Lightbulb; a people-only room has no agent to name.
    if (!front) return members.some((m) => m.kind === "agent") ? "Lightbulb may reply" : "";
    members = [front];
  }
  const mentioned = new Set(mentions.filter((ref) => ref.kind === "agent").map((ref) => ref.id));
  const parts: string[] = [];
  for (const member of members) {
    if (member.kind !== "agent") continue;
    const listenMode = member.listenMode;
    // observe ("Off") never runs — not on ambient traffic, not when mentioned, not in a DM.
    if (listenMode === "observe") { parts.push(`${member.displayName} is off`); continue; }
    // The dispatch decision comes FIRST: an agent this message will not be
    // dispatched to cannot be steered by it, however busy it looks.
    const addressed = roomKind === "dm" || mentioned.has(member.id);
    const willReply = addressed || listenMode === "active";
    if (!willReply) parts.push(`${member.displayName} when mentioned`);
    else if (runningAgentIds.includes(member.id)) parts.push(`${member.displayName} is working — sending now steers that run`);
    // An unaddressed active ("Listening") agent passes the cheap relevance gate first.
    // One voice says it one way everywhere (W13): the Router may hand the ask to a specialist, or answer none.
    else parts.push(`${member.displayName} ${addressed && !oneVoice ? "will" : "may"} reply`);
  }
  return parts.join(" · ");
}
