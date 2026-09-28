/**
 * Sent → Delivered → Read / Read by N under the viewer's last message, from the
 * room detail's receipts (`readCursors`, `agentDeliveries`, started runs) kept
 * live by `room.read.changed` / `room.delivery.changed` (see useRooms).
 * "Read" for an agent = its run on the message started; a later reply also
 * proves a read (an agent's only when it is a room-turn reply).
 */
import type { CanonicalRoomMessage, RoomKind, RoomMember } from "./types";
import type { RoomReceipts } from "./useRooms";

export type ReadState = { kind: "sent" } | { kind: "delivered" } | { kind: "read" } | { kind: "read-by"; count: number };

type Seen = Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "runId">;

export function readState({
  message, roomKind, messages, receipts, members, viewerId, ignoreHumanCursors = false,
}: {
  message: Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "mentions" | "addressedAgentIds">;
  roomKind: RoomKind;
  messages: readonly Seen[];
  receipts: RoomReceipts;
  members: readonly Pick<RoomMember, "kind" | "id">[];
  viewerId: string;
  /**
   * A human read cursor is ROOM-wide: it can pass a thread reply the member never
   * opened the thread to see, just by reading a later top-level message. Set this
   * for a thread's replies so "Read"/"Read by N" reflects only positive per-thread
   * evidence — a later reply in the thread, or an agent read/reply — never a human
   * cursor. Room-level callers must leave this false.
   */
  ignoreHumanCursors?: boolean;
}): ReadState {
  const recipients = members.filter((m) => !(m.kind === "human" && m.id === viewerId));
  const isRecipient = (kind: string, id: string) => recipients.some((m) => m.kind === kind && m.id === id);
  const readers = new Set<string>();
  if (!ignoreHumanCursors) {
    for (const [userId, seq] of receipts.readCursors) {
      if (seq >= message.messageSeq && isRecipient("human", userId)) readers.add(`human:${userId}`);
    }
  }
  for (const [agentId, ids] of receipts.agentReads) {
    if (ids.has(message.messageId) && isRecipient("agent", agentId)) readers.add(`agent:${agentId}`);
  }
  for (const later of messages) {
    if (later.messageSeq <= message.messageSeq) continue;
    const { kind, id } = later.author;
    if (kind === "system" || !isRecipient(kind, id) || (kind === "agent" && later.runId === null)) continue;
    readers.add(`${kind}:${id}`);
  }
  if (readers.size > 0) return roomKind === "dm" ? { kind: "read" } : { kind: "read-by", count: readers.size };
  // The agents this message addresses, when the message carries the field: it
  // also covers a name-addressed open ("Hey Friday, ..."), which is not an
  // @mention, but excludes an `active` listener Jev was merely eligible to
  // route -- Jev picks at most one of them (or none). Absent (a backend/cache
  // older than this field) falls back to the previous @mention/DM guess.
  const addressed = (agentId: string) =>
    message.addressedAgentIds !== undefined
      ? message.addressedAgentIds.includes(agentId)
      : roomKind === "dm" || message.mentions.some((ref) => ref.kind === "agent" && ref.id === agentId);
  for (const [agentId, seq] of receipts.agentDeliveries) {
    if (seq >= message.messageSeq && isRecipient("agent", agentId) && addressed(agentId)) return { kind: "delivered" };
  }
  return { kind: "sent" };
}

export const readStateLabel = (state: ReadState): string =>
  state.kind === "sent" ? "Sent" : state.kind === "delivered" ? "Delivered" : state.kind === "read" ? "Read" : `Read by ${state.count}`;

/** The viewer's newest live, canonical (seq > 0) message: the one that carries the read state. */
export function lastOwnMessageId(
  messages: readonly Pick<CanonicalRoomMessage, "messageId" | "messageSeq" | "author" | "deletedAt">[],
  viewerId: string,
): string | null {
  let best: { id: string; seq: number } | null = null;
  for (const m of messages) {
    if (m.author.kind !== "human" || m.author.id !== viewerId || m.deletedAt || m.messageSeq <= 0) continue;
    if (!best || m.messageSeq > best.seq) best = { id: m.messageId, seq: m.messageSeq };
  }
  return best?.id ?? null;
}

/**
 * An agent message the agent sent on its own. The backend stamps `runId` only
 * on a room-turn reply (`post_agent_message`) and exposes no finer kind, so
 * nightly check-ins, routines and finished-session notes share one label
 * (owner ruling 2026-09-26).
 */
export const UNPROMPTED_LABEL = "on its own";

export const unpromptedLabel = (message: Pick<CanonicalRoomMessage, "author" | "runId" | "deletedAt">): string | null =>
  message.author.kind === "agent" && message.runId === null && !message.deletedAt ? UNPROMPTED_LABEL : null;
