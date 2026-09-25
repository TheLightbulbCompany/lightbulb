import type { RoomKind } from "@lightbulb/shared";

/**
 * What the Messages workspace is showing, for the run notifications that live
 * outside its provider (desktop mounts them beside the dock): which room is on
 * screen, and every room's kind + name for line 1 ("Pulse · #GTM Strategy").
 * Written by MessageNotifications, which the provider mounts.
 */
export interface RoomView {
  visibleRoomId: string | null;
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
