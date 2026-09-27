import type { RoomActivityChanged } from "../live/types";
import type { RoomKind } from "./types";

/** Every room's live activity, keyed by room then by `${actor.kind}:${actor.id}` → latest hint. */
export type RoomActivity = ReadonlyMap<string, ReadonlyMap<string, RoomActivityChanged>>;

export function applyRoomActivity(current: RoomActivity, payload: RoomActivityChanged): RoomActivity {
  const room = new Map(current.get(payload.roomId) ?? []);
  const key = `${payload.actor.kind}:${payload.actor.id}`;
  if (payload.state === "idle") room.delete(key);
  else room.set(key, payload);
  const next = new Map(current);
  if (room.size) next.set(payload.roomId, room);
  else next.delete(payload.roomId);
  return next;
}

/** `expiresAt` is authoritative for BOTH "typing" and "working" (a "working" hint is
 *  refreshed by every run heartbeat, per the controller ruling); an entry with no
 *  `expiresAt` never expires on its own — only an "idle" event clears it. */
const live = (entry: RoomActivityChanged, now: number) =>
  entry.expiresAt === undefined || Date.parse(entry.expiresAt) > now;

export function pruneRoomActivity(current: RoomActivity, now: number): RoomActivity {
  let changed = false;
  const next = new Map<string, ReadonlyMap<string, RoomActivityChanged>>();
  for (const [roomId, room] of current) {
    const kept = new Map([...room].filter(([, entry]) => live(entry, now)));
    if (kept.size !== room.size) changed = true;
    if (kept.size) next.set(roomId, kept);
  }
  return changed ? next : current;
}

/** Web `RoomPanel`'s `.room-typing` sentence, verbatim ("and others", the ellipsis character). */
export function typingLine(names: readonly string[]): string {
  if (names.length === 0) return "";
  const shown = names.slice(0, 3).join(", ");
  const overflow = names.length > 3 ? " and others" : "";
  return `${shown}${overflow} ${names.length === 1 ? "is" : "are"} typing…`;
}

/** The Chats row's live line, or null: typing wins over working. */
export function roomLiveLine(current: RoomActivity, roomId: string, roomKind: RoomKind, now = Date.now()): string | null {
  const entries = [...(current.get(roomId)?.values() ?? [])].filter((entry) => live(entry, now));
  const typing = entries.filter((entry) => entry.state === "typing").map((entry) => entry.actor.displayName);
  if (typing.length) return typingLine(typing);
  const working = entries.filter((entry) => entry.state === "working").map((entry) => entry.actor.displayName);
  if (!working.length) return null;
  if (roomKind === "dm") return "working…";
  return `${working.slice(0, 2).join(", ")}${working.length > 2 ? " and others" : ""} ${working.length === 1 ? "is" : "are"} working…`;
}
