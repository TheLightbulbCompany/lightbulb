/**
 * Shared "which agent chat (and session) is currently on screen" signal.
 *
 * The push notification handler reads this at handle-time to suppress the banner
 * for a reply the user is actively watching stream in. The chat screen sets it
 * on focus and clears it on blur (see agent/[agentId].tsx); session switches
 * update the session half while focused. Module-level rather than context so
 * the notification handler — which lives outside React render — can read the
 * latest value synchronously.
 */
let currentAgentId: string | null = null;
// null == the General session (matches the chat screen's activeSessionId).
let currentSessionId: string | null = null;

export function setCurrentAgentId(id: string | null): void {
  currentAgentId = id;
  if (id === null) currentSessionId = null;
}

export function getCurrentAgentId(): string | null {
  return currentAgentId;
}

export function setCurrentSessionId(id: string | null): void {
  currentSessionId = id;
}

export function getCurrentChat(): { agentId: string | null; sessionId: string | null } {
  return { agentId: currentAgentId, sessionId: currentSessionId };
}
