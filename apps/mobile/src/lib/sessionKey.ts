// Port of the web's lib/sessionKey.ts (sessions contract §1).
//
// Mirrors the backend guard (websocket_chat.py `_SESSION_ID_RE`): strict
// lowercase RFC-4122 v4. OpenClaw lowercases the whole key before
// store/lookup, so a mixed-case id would alias-merge two sessions.
export const SESSION_ID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Build the OpenClaw session key used for chat.history / sessions.* RPCs.
 * `null` sessionId → the legacy "General" 3-part key; otherwise the validated
 * 4-part key. The backend re-validates on the write path; this just keeps the
 * client from targeting a malformed key.
 */
export function buildChatSessionKey(
  agentId: string,
  userId: string,
  sessionId: string | null,
): string {
  if (sessionId === null) return `agent:${agentId}:${userId}`;
  if (!SESSION_ID_RE.test(sessionId)) {
    throw new Error(`Invalid session id: ${sessionId}`);
  }
  return `agent:${agentId}:${userId}:${sessionId}`;
}

/**
 * Extract the session id from a 4-part key (`agent:{agentId}:{member}:{id}`).
 * 3-part/General keys — and non-session trailing segments like cron ids or
 * member ids — return null. Push payloads carry the key OpenClaw echoed
 * (lowercased), which is exactly the form SESSION_ID_RE accepts.
 */
export function sessionIdFromKey(sessionKey: string | null | undefined): string | null {
  if (!sessionKey) return null;
  const last = sessionKey.split(":").pop() ?? "";
  return SESSION_ID_RE.test(last) ? last : null;
}
