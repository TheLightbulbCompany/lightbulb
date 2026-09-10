// Single-slot handoff for "focus the composer of agent X" across a panel
// mount boundary. The Get Started chat action can OPEN a brand-new chat dock
// panel — at dispatch time that panel's composer may not be mounted in the
// selected room. Desktop navigation defers the slot until ChatPanel confirms
// that room is rendered; mobile and already-selected callers can still use
// the mount/event paths directly.
//
// The request may carry a DRAFT: the Brain's "Ask Friday" exits hand the
// composer a pre-written message, seeded only into an empty composer.
//
// One pending slot only (a newer request replaces an older one) with a short
// expiry so a request whose panel never materializes can't focus a composer
// minutes later.

const PENDING_TTL_MS = 5_000;

export interface FocusComposerRequest {
  /** Text to seed the composer with (the Brain's ASK / "file the inbox"). */
  draft?: string;
}

let _pending: { agentId: string; draft?: string; requestedAt: number; deferred: boolean } | null = null;

export function requestFocusComposer(agentId: string, draft?: string): void {
  _pending = { agentId, draft, requestedAt: Date.now(), deferred: false };
}

export function deferFocusComposerRequest(agentId: string): void {
  if (_pending?.agentId === agentId) _pending.deferred = true;
}

export function releaseFocusComposerRequest(agentId: string): boolean {
  if (!_pending || _pending.agentId !== agentId) return false;
  _pending.requestedAt = Date.now();
  _pending.deferred = false;
  return true;
}

export function cancelFocusComposerRequest(agentId: string): void {
  if (_pending?.agentId === agentId) _pending = null;
}

/** Consume the pending request for this agent. Non-null exactly once per
 *  request, and only for the requested agent within the expiry window. */
export function consumeFocusComposerRequest(agentId: string): FocusComposerRequest | null {
  if (!_pending) return null;
  if (_pending.deferred) return null;
  if (Date.now() - _pending.requestedAt > PENDING_TTL_MS) {
    _pending = null;
    return null;
  }
  if (_pending.agentId !== agentId) return null;
  const { draft } = _pending;
  _pending = null;
  return draft === undefined ? {} : { draft };
}

export function _resetFocusComposerRequestForTests(): void {
  _pending = null;
}
