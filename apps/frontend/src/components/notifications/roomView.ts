import type { RoomKind } from "@lightbulb/shared";

/**
 * What the Messages workspace is showing, for the run notifications that live
 * outside its provider (desktop mounts them beside the dock): which room is on
 * screen, and every room's kind + name for line 1 ("Pulse · #GTM Strategy").
 * Written by MessageNotifications, which the provider mounts.
 */
export interface RoomView {
  visibleRoomId: string | null;
  /** Rooms on screen in a side column (session chat, an app's conversation), besides the main one. */
  sideRoomIds?: readonly string[];
  rooms: ReadonlyMap<string, { kind: RoomKind; name: string | null }>;
}

let view: RoomView = { visibleRoomId: null, rooms: new Map() };
const listeners = new Set<() => void>();

export function setRoomView(next: RoomView): void {
  view = next;
  for (const listener of listeners) listener();
}

export function getRoomView(): RoomView {
  return view;
}

export function subscribeRoomView(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// One notification per agent run: its reply's message banner and the run's own
// "finished"/"failed" banner are the same news. Whichever arrives first claims
// the run; the other stays quiet.
const claimedRuns = new Set<string>();

export function claimRunNotice(runId: string): boolean {
  if (claimedRuns.has(runId)) return false;
  claimedRuns.add(runId);
  if (claimedRuns.size > 500) claimedRuns.delete(claimedRuns.values().next().value!);
  return true;
}
