/**
 * OpenClaw session-key parsing — the one place the wire format is decoded.
 *
 * Shapes (openclaw/src/routing/session-key.ts, mirrored by the backend's
 * `_parse_session_key` in core/gateway/connection_pool.py):
 *
 *   Personal webchat   agent:<agentId>:main
 *   Org webchat        agent:<agentId>:<clerkUserId>
 *   Named webchat      agent:<agentId>:<clerkUserId>:<uuid>
 *   Cron               agent:<agentId>:cron:<cronId>(:run:<runId>)?
 *   Paperclip          agent:<agentId>:paperclip(:issue:<id>|:run:<id>)?
 *   Channel DM         agent:<agentId>:<channel>:<accountId>:direct:<peerId>
 *   Channel group/room agent:<agentId>:<channel>:(group|channel):<id>…
 *
 * The member id is the THIRD segment, never the trailing one — a named
 * webchat session ends in a client-minted uuid.
 */

export type SessionSource = "webchat" | "cron" | "paperclip" | "channel";

export interface ParsedSessionKey {
  agentId: string;
  source: SessionSource;
  /** Clerk user id for org webchat. Absent for personal (`main`) sessions. */
  memberId?: string;
}

export function parseSessionKey(sessionKey: string | undefined | null): ParsedSessionKey | null {
  if (!sessionKey) return null;
  const parts = sessionKey.split(":");
  if (parts.length < 3 || parts[0] !== "agent") return null;
  const agentId = parts[1];
  if (!agentId) return null;
  if (parts[2] === "cron") return { agentId, source: "cron" };
  if (parts[2] === "paperclip") return { agentId, source: "paperclip" };
  // 3 or 4 parts is webchat: cron/paperclip 4-part keys are handled above and
  // every channel key is 5+ segments.
  if (parts.length <= 4) {
    return parts[2] === "main"
      ? { agentId, source: "webchat" }
      : { agentId, source: "webchat", memberId: parts[2] };
  }
  return { agentId, source: "channel" };
}

/**
 * True when a gateway request carrying `sessionKey` belongs to the signed-in
 * operator's own web chat.
 *
 * The backend broadcasts question/approval events to every frontend of an
 * owner (the generic event branch has no per-member target), so without this
 * filter member B sees — and can answer — member A's prompt.
 *
 * A key that doesn't parse (or is absent) is ACCEPTED: it carries no scoping
 * signal, and dropping it would silence legitimate prompts from emitters that
 * don't populate one. A key that parses to a non-webchat source is DROPPED:
 * cron/channel/Paperclip runs are other surfaces' business.
 */
export function isSessionKeyForUser(
  sessionKey: string | undefined | null,
  userId: string | null | undefined,
): boolean {
  if (!userId) return false; // not signed in — drop everything
  const parsed = parseSessionKey(sessionKey);
  if (!parsed) return true; // no scoping signal
  if (parsed.source !== "webchat") return false;
  if (!parsed.memberId) return true; // personal `main` session
  return parsed.memberId.toLowerCase() === userId.toLowerCase();
}
