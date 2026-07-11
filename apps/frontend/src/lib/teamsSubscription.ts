/**
 * Refcounted ownership of the per-connection `teams.subscribe` registration.
 *
 * The backend teams broker tracks subscriptions per WebSocket CONNECTION id,
 * and every consumer in a tab shares one gateway connection. A consumer that
 * sends `teams.unsubscribe` on unmount therefore kills the subscription for
 * every other consumer on the same socket. All subscribe/unsubscribe traffic
 * must flow through this module instead of calling `send` directly.
 *
 * Consumer contract (two effects, IN THIS ORDER):
 *   1. acquire on mount, release on unmount — acquire never sends; it only
 *      registers interest.
 *   2. resubscribe whenever isConnected is true (mount + every reconnect) —
 *      the only place subscribe frames are emitted, so nothing is sent while
 *      the socket is closed (`send` would silently drop it anyway).
 * Duplicate subscribe sends are safe — the broker's subscribe is idempotent.
 */
export type GatewaySend = (payload: unknown) => boolean;

let refCount = 0;

export function acquireTeamsEvents(): void {
  refCount += 1;
}

export function releaseTeamsEvents(send: GatewaySend): void {
  if (refCount === 0) return;
  refCount -= 1;
  if (refCount === 0) {
    send({ type: "teams.unsubscribe" });
  }
}

export function resubscribeTeamsEvents(send: GatewaySend): void {
  if (refCount > 0) {
    send({ type: "teams.subscribe" });
  }
}

export function _resetTeamsEventsForTests(): void {
  refCount = 0;
}
