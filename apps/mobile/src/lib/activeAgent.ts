/**
 * Shared "which agent chat is currently on screen" signal.
 *
 * The push notification handler reads this at handle-time to suppress the banner
 * for a reply the user is actively watching stream in. The chat screen sets it
 * on focus and clears it on blur (see agent/[agentId].tsx). Module-level rather
 * than context so the notification handler — which lives outside React render —
 * can read the latest value synchronously.
 */
let currentAgentId: string | null = null;

export function setCurrentAgentId(id: string | null): void {
  currentAgentId = id;
}

export function getCurrentAgentId(): string | null {
  return currentAgentId;
}
