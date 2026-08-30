import "server-only";

import { auth } from "@clerk/nextjs/server";

import { adminHttp } from "@/app/admin/_lib/api";

/**
 * Shared HTTP helpers for admin Server Actions.
 *
 * Five sibling files previously each maintained their own near-identical
 * `adminPost` (~50 LOC each — same Clerk-token fetch, same Idempotency-Key
 * generation, same JSON / 4xx unwrap, same try/catch envelope). Centralising
 * the request shape removes a drift surface — every action that adds a
 * header / changes the error envelope only needs to touch this file.
 *
 * **Not a `"use server"` module.** If `adminPost(path, body)` were itself a
 * Server Action, a client that obtains its server-reference id could pass
 * ANY path / body and have the frontend forward the caller's Clerk token to
 * arbitrary backend endpoints. This file is `server-only` (enforced at build
 * time by the `server-only` package) so it can be imported from the
 * per-endpoint `"use server"` files but cannot itself be invoked from the
 * client.
 */

export interface ActionResult {
  ok: boolean;
  status: number;
  data?: unknown;
  error?: string;
}

async function adminRequest(
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  body?: object,
): Promise<ActionResult> {
  const { getToken } = await auth();
  const token = await getToken();
  if (!token) {
    return { ok: false, status: 401, error: "missing_token" };
  }

  const { ok, status, data } = await adminHttp({
    method,
    path,
    token,
    body: method === "POST" || method === "PUT" ? body : undefined,
    idempotent: method !== "GET",
  });

  if (!ok) {
    return {
      ok: false,
      status,
      data,
      error:
        (data && typeof data === "object" && "detail" in data
          ? String((data as { detail?: unknown }).detail)
          : undefined) ?? (status === 0 ? "network_error" : `http_${status}`),
    };
  }
  return { ok: true, status, data };
}

export async function adminGet(path: string): Promise<ActionResult> {
  return adminRequest("GET", path);
}

export async function adminPost(path: string, body?: object): Promise<ActionResult> {
  return adminRequest("POST", path, body);
}

export async function adminPut(path: string, body?: object): Promise<ActionResult> {
  return adminRequest("PUT", path, body);
}

export async function adminDelete(path: string): Promise<ActionResult> {
  return adminRequest("DELETE", path);
}
