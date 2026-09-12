/**
 * Shared "what is currently on screen" signal for the push handler.
 *
 * The notification handler reads this at handle-time to suppress the banner
 * for a message the user is already watching arrive. The room and thread
 * screens set their half on focus and clear it on blur; the agent half is kept
 * for legacy agent/session pushes. Module-level rather than context so the
 * handler — which lives outside React render — can read the latest value
 * synchronously.
 */
export interface CurrentTarget {
  agentId: string | null;
  sessionId: string | null;
  roomId: string | null;
  threadId: string | null;
}

let target: CurrentTarget = {
  agentId: null,
  sessionId: null,
  roomId: null,
  threadId: null,
};

export function setCurrentTarget(next: Partial<CurrentTarget>): void {
  target = { ...target, ...next };
}

export function getCurrentTarget(): CurrentTarget {
  return { ...target };
}

/** Monotonic id of the live claim. Identity, not value: two stacked screens can
 * hold the SAME roomId (a push for the room you are already in pushes a second
 * copy of it), and comparing by value would let the older one's blur clear the
 * newer one's claim. */
let claim = 0;

/**
 * Claim the target for a room (or thread) screen and hand back its blur
 * cleanup.
 *
 * The cleanup clears only while this screen's claim is still the live one:
 * expo-router does not order a blurring screen's cleanup against the newly
 * focused screen's claim, so an unconditional clear would wipe the thread's
 * claim on the way in — or the room's on the way back — and banner a message
 * that is on screen (or, worse, suppress one that is not).
 */
export function claimRoomTarget(roomId: string | null, threadId: string | null): () => void {
  const token = ++claim;
  setCurrentTarget({ roomId, threadId });
  return () => {
    if (claim === token) setCurrentTarget({ roomId: null, threadId: null });
  };
}
