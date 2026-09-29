// Session-key parsing for legacy push payloads (sessions contract §1).
//
// Shared rather than per-client on purpose: this regex mirrors a BACKEND guard
// (`routers/websocket_chat.py:_SESSION_ID_RE`) — strict lowercase RFC-4122 v4.
// OpenClaw lowercases the whole key before store/lookup, so a mixed-case id
// would alias-merge two sessions. A client copy that drifts from the backend's
// shape is a correctness bug, not a style one, which is why there must be
// exactly one copy per client codebase and not one per app.

export const SESSION_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

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
