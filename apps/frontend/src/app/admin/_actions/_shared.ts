"use server";

import { randomUUID } from "crypto";

import { auth } from "@clerk/nextjs/server";

import { apiUrl } from "@/app/admin/_lib/url";

/**
 * Shared HTTP helpers for admin Server Actions.
 *
 * Five sibling files previously each maintained their own near-identical
 * `adminPost` (~50 LOC each — same Clerk-token fetch, same Idempotency-Key
 * generation, same JSON / 4xx unwrap, same try/catch envelope). Centralising
 * the request shape removes a drift surface — every action that adds a
 * header / changes the error envelope only needs to touch this file.
 *
 * `"use server"` files can only export async functions. `ActionResult` is
 * an interface (erased at compile time), so defining it here is safe; do
 * NOT add `export type { ActionResult }` re-exports from sibling files —
 * Turbopack turns those into runtime ReferenceErrors on SSR chunk load.
 */

export interface ActionResult {
  ok: boolean;
  status: number;
  data?: unknown;
  error?: string;
}

async function adminRequest(
  method: "GET" | "POST",
  path: string,
  body?: object,
): Promise<ActionResult> {
  const { getToken } = await auth();
  const token = await getToken();
  if (!token) {
    return { ok: false, status: 401, error: "missing_token" };
  }

  const base = apiUrl().replace(/\/+$/, "");
  const url = `${base}${path.startsWith("/") ? path : `/${path}`}`;

  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
  // Dedupe double-clicks server-side on mutation paths.
  if (method === "POST") {
    headers["Idempotency-Key"] = randomUUID();
  }

  try {
    const res = await fetch(url, {
      method,
      headers,
      body: method === "POST" && body ? JSON.stringify(body) : undefined,
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

export async function adminGet(path: string): Promise<ActionResult> {
  return adminRequest("GET", path);
}

export async function adminPost(path: string, body?: object): Promise<ActionResult> {
  return adminRequest("POST", path, body);
}
