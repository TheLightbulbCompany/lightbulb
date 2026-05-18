"use server";

import { adminPost, type ActionResult } from "./_shared";

/**
 * Server Actions for the per-user container surface (admin dashboard).
 *
 * Each action POSTs to a backend `/admin/users/{user_id}/container/*` route
 * with a fresh `Idempotency-Key` so double-clicks short-circuit on the
 * backend (CEO D1). The Clerk JWT is fetched server-side via `auth()`; never
 * exposed to the client. We always return the parsed JSON response (or a
 * `{error}` shape) so the calling client component can surface errors inline.
 */

export async function reprovisionContainer(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/container/reprovision`);
}

export async function stopContainer(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/container/stop`);
}

export async function startContainer(userId: string): Promise<ActionResult> {
  return adminPost(`/admin/users/${encodeURIComponent(userId)}/container/start`);
}

export async function resizeContainer(userId: string): Promise<ActionResult> {
  return adminPost(
    `/admin/users/${encodeURIComponent(userId)}/container/resize`,
  );
}

/**
 * Queue a Track 2 image update for an owner. Targets the thin
 * `/admin/users/{user_id}/container/queue-image-update` endpoint,
 * which resolves user_id to owner_id server-side (handles both
 * personal users where they're equal, and org members where the
 * billing owner is the org). Mirrors the resolve pattern used by
 * the rest of the admin container actions.
 */
export async function queueImageUpdate(
  userId: string,
  newImage: string,
  description?: string,
): Promise<ActionResult> {
  return adminPost(
    `/admin/users/${encodeURIComponent(userId)}/container/queue-image-update`,
    {
      new_image: newImage,
      ...(description?.trim() ? { description: description.trim() } : {}),
    },
  );
}

