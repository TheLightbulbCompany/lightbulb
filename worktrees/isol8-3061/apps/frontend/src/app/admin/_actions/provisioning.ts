"use server";

import { adminPost, type ActionResult } from "./_shared";

/**
 * Server Actions for the per-user provisioning levers (admin dashboard).
 *
 * Each action POSTs to a backend `/admin/users/{user_id}/provisioning/*`
 * route with a fresh `Idempotency-Key` so double-clicks short-circuit on the
 * backend (CEO D1). The Clerk JWT is fetched server-side via `auth()`; never
 * exposed to the client. Mirrors `_actions/container.ts`'s idiom exactly.
 */

export async function retryProvision(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/provisioning/retry`);
}
