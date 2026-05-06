"use client";

import { useAuth } from "@clerk/nextjs";
import { useEffect } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";
const SESSION_KEY = "isol8.users.synced";

/**
 * Invisible client component that calls POST /users/sync once per browser
 * session after Clerk auth resolves. The main app at app.isol8.co does this
 * via its own ChatLayout mount; the marketplace storefront is a separate
 * Next.js app, so without this users have no `users` DDB row, which breaks
 * downstream lookups (billing tier, /my-agents, etc).
 *
 * Idempotent on the backend side. Guarded here by sessionStorage to avoid
 * a redundant call on every page nav.
 */
export function UserSync() {
  const { isSignedIn, getToken } = useAuth();

  useEffect(() => {
    if (!isSignedIn) return;
    if (typeof window === "undefined") return;
    if (window.sessionStorage.getItem(SESSION_KEY) === "1") return;

    let cancelled = false;
    (async () => {
      try {
        const jwt = await getToken();
        if (!jwt || cancelled) return;
        const resp = await fetch(`${API}/users/sync`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${jwt}`,
            "content-type": "application/json",
          },
          body: "{}",
        });
        if (resp.ok) {
          window.sessionStorage.setItem(SESSION_KEY, "1");
        }
      } catch {
        // Non-fatal; we'll retry on the next session.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isSignedIn, getToken]);

  return null;
}
