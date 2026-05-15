"use server";

import { randomUUID } from "crypto";

import { auth } from "@clerk/nextjs/server";

import { apiUrl } from "@/app/admin/_lib/url";

/**
 * Server Actions for the per-user container surface (admin dashboard).
 *
 * Each action POSTs to a backend `/admin/users/{user_id}/container/*` route
 * with a fresh `Idempotency-Key` so double-clicks short-circuit on the
 * backend (CEO D1). The Clerk JWT is fetched server-side via `auth()`; never
 * exposed to the client. We always return the parsed JSON response (or a
 * `{error}` shape) so the calling client component can surface errors inline.
 */

interface ActionResult {
  ok: boolean;
  status: number;
  data?: unknown;
  error?: string;
}

async function adminPost(path: string, body?: object): Promise<ActionResult> {
  const { getToken } = await auth();
  const token = await getToken();
  if (!token) {
    return { ok: false, status: 401, error: "missing_token" };
  }

  const base = apiUrl().replace(/\/+$/, "");
  const url = `${base}${path.startsWith("/") ? path : `/${path}`}`;

  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "Idempotency-Key": randomUUID(),
      },
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    const text = await res.text();
    let data: unknown = undefined;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
    if (!res.ok) {
      return {
        ok: false,
        status: res.status,
        data,
        error:
          (data && typeof data === "object" && "detail" in data
            ? String((data as { detail?: unknown }).detail)
            : undefined) ?? `http_${res.status}`,
      };
    }
    return { ok: true, status: res.status, data };
  } catch (e) {
    return {
      ok: false,
      status: 0,
      error: e instanceof Error ? e.message : "network_error",
    };
  }
}

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
 * Queue a Track 2 image update for an owner. Targets the existing
 * `POST /api/v1/updates` endpoint (platform-admin-gated since #653).
 *
 * `ownerId` here is the openclaw billing owner: equal to `user_id` for
 * personal accounts, equal to the `org_id` for org members. The admin
 * dashboard's per-user container page passes the route's `user_id`
 * which works for personal users (the dev-test case). Org-member
 * support needs a server-side resolve step — add when needed.
 */
export async function queueImageUpdate(
  ownerId: string,
  newImage: string,
  description?: string,
): Promise<ActionResult> {
  return adminPost(`/updates`, {
    owner_id: ownerId,
    update_type: "image_update",
    description: description?.trim() || `Image update to ${newImage}`,
    changes: { new_image: newImage },
  });
}

// NOTE: see catalog.ts — "use server" files must export ONLY async functions.
// Turbopack turns `export type { ActionResult }` into a runtime ReferenceError
// at SSR chunk load. No consumer imports ActionResult today.
