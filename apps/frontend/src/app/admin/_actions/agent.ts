"use server";

import { adminPost, type ActionResult } from "./_shared";

/**
 * Server Actions for the per-agent admin surface.
 *
 * The backend audits the call. Errors come back as an ActionResult so the
 * calling client component can render them inline rather than throwing.
 */

export async function deleteAgent(
  userId: string,
  agentId: string,
): Promise<ActionResult> {
  return adminPost(
    `/admin/users/${encodeURIComponent(userId)}/agents/${encodeURIComponent(agentId)}/delete`,
  );
}
