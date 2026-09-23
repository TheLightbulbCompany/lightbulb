import type { ListenMode, MemberRef, RoomKind } from "./types";

/**
 * What people see for each stored listen mode. The stored values predate these
 * names: `active` shows as "Listening" (the paid default — every agent
 * listens, and Jev, a routing model, picks at most one responder per message)
 * and `observe` as "Off" (never runs). No label reuses a stored value, so the
 * two vocabularies cannot be confused.
 */
export const LISTEN_MODE_LABELS: Record<ListenMode, string> = { active: "Listening", mentions: "Mentions", observe: "Off" };

/** The Response `<select>` options. Listening is paid: when the workspace lacks it the option stays visible but locked. */
export function listenModeOptions(listeningAvailable: boolean): { value: ListenMode; label: string; disabled: boolean }[] {
  return (Object.entries(LISTEN_MODE_LABELS) as [ListenMode, string][]).map(([value, label]) => {
    const disabled = value === "active" && !listeningAvailable;
    return { value, label: disabled ? `${label} (paid plan)` : label, disabled };
  });
}

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
  listeningAvailable = true,
): string {
  const mentioned = new Set(mentions.filter((ref) => ref.kind === "agent").map((ref) => ref.id));
  const parts: string[] = [];
  for (const member of members) {
    if (member.kind !== "agent") continue;
    // Listening is paid: on the free tier a stored Listening runs as Mentions
    // (the dispatcher's plan gate), so the hint says what will actually happen.
    const listenMode = member.listenMode === "active" && !listeningAvailable ? "mentions" : member.listenMode;
    // observe ("Off") never runs — not on ambient traffic, not when mentioned, not in a DM.
    if (listenMode === "observe") { parts.push(`${member.displayName} is off`); continue; }
    // The dispatch decision comes FIRST: an agent this message will not be
    // dispatched to cannot be steered by it, however busy it looks.
    const addressed = roomKind === "dm" || mentioned.has(member.id);
    const willReply = addressed || listenMode === "active";
    if (!willReply) parts.push(`${member.displayName} when mentioned`);
    else if (runningAgentIds.includes(member.id)) parts.push(`${member.displayName} is working — sending now steers that run`);
    // An unaddressed active ("Listening") agent passes the cheap relevance gate first.
    else parts.push(`${member.displayName} ${addressed ? "will" : "may"} reply`);
  }
  return parts.join(" · ");
}
