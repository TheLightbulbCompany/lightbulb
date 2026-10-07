"use server";

import { adminPost, type ActionResult } from "./_shared";

/**
 * Server Actions for the per-agent admin surface.
 *
 * Both endpoints proxy a gateway RPC into the user's container; the backend
 * audits the call and returns a 409 when the container isn't running. We
 * surface that to the calling client component so it can render an inline
 * error rather than throwing.
 */

export async function deleteAgent(
  userId: string,
  agentId: string,
): Promise<ActionResult> {
  return adminPost(
    `/admin/users/${encodeURIComponent(userId)}/agents/${encodeURIComponent(agentId)}/delete`,
  );
}

export async function clearAgentSessions(
  userId: string,
  agentId: string,
): Promise<ActionResult> {
  return adminPost(
    `/admin/users/${encodeURIComponent(userId)}/agents/${encodeURIComponent(agentId)}/clear-sessions`,
  );
}

