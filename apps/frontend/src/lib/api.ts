import { useMemo } from "react";
import { useAuth } from "@clerk/nextjs";

// Same origin: next.config.ts rewrites /api/v1/* to this environment's backend.
const BACKEND_URL = "/api/v1";

/** Authenticated JSON POST to the backend, with the Clerk bearer token. */
export function useApi(): { post: (endpoint: string, body: unknown) => Promise<unknown> } {
  const { getToken } = useAuth();

  return useMemo(
    () => ({
      async post(endpoint: string, body: unknown): Promise<unknown> {
        const token = await getToken();
        if (!token) throw new Error("No authentication token available");
        const response = await fetch(`${BACKEND_URL}${endpoint}`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!response.ok) throw new Error(`API ${response.status}`);
        return response.status === 204 ? null : response.json();
      },
    }),
    [getToken],
  );
}
