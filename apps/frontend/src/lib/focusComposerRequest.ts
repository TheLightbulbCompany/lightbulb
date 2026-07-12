// Single-slot handoff for "focus the composer of agent X" across a panel
// mount boundary. The Get Started chat action can OPEN a brand-new chat dock
// panel — at dispatch time that panel's ChatInput hasn't mounted or
// registered its window listener, so a CustomEvent alone is lost. The shell
// records the intent here BEFORE opening; the composer consumes it from its
// mount/agent-resolution effect. The window-event path (already-mounted
// composer) consumes the slot too, so the two delivery paths never
// double-fire.
//
// One pending slot only (a newer request replaces an older one) with a short
// expiry so a request whose panel never materializes can't focus a composer
// minutes later.

const PENDING_TTL_MS = 5_000;

let _pending: { agentId: string; requestedAt: number } | null = null;

export function requestFocusComposer(agentId: string): void {
  _pending = { agentId, requestedAt: Date.now() };
}

/** Consume the pending request for this agent. Returns true exactly once per
 *  request, and only for the requested agent within the expiry window. */
export function consumeFocusComposerRequest(agentId: string): boolean {
  if (!_pending) return false;
  if (Date.now() - _pending.requestedAt > PENDING_TTL_MS) {
    _pending = null;
    return false;
  }
  if (_pending.agentId !== agentId) return false;
  _pending = null;
  return true;
}

export function _resetFocusComposerRequestForTests(): void {
  _pending = null;
}
